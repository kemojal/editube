"""Task D6: `app/services/video_versions.py` — the shared version-chain
helper behind both the multipart upload route's `version_of` path and
rough-cut export's "save as new version".

Covers:
- resolve_version_chain: fresh group backfill (source predates
  version_group_id) computes version 2 (the source itself counts as v1 in
  its own freshly-backfilled group); existing group with siblings ->
  max+1; backfill mutates the source video's version_group_id in place.
- register_video_version: creates the Video row in the same project/folder
  as the source, correct version/version_group_id, no VideoTranscription
  seeded, thumbnail job enqueued only when no thumbnail_url was supplied.
"""

from __future__ import annotations

import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import ActivityFeed, Folder, Project, User, Video, VideoTranscription
from app.services.video_versions import register_video_version, resolve_version_chain


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(type_, compiler, **kw):
    # Tests run against in-memory SQLite; JSONB columns degrade to plain JSON.
    return "JSON"


class _SqliteDbTestCase(unittest.TestCase):
    tables = [
        User.__table__,
        Project.__table__,
        Folder.__table__,
        Video.__table__,
        VideoTranscription.__table__,
        # register_video_version now logs a real activity entry.
        ActivityFeed.__table__,
    ]

    def setUp(self) -> None:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine, tables=self.tables)
        self.db = sessionmaker(bind=engine)()

        self.user = User(email="editor@example.com", name="Edna Editor", role="creator")
        self.db.add(self.user)
        self.db.flush()

        self.project = Project(name="Launch", creator_id=self.user.id, workspace_id=1)
        self.db.add(self.project)
        self.db.flush()

    def tearDown(self) -> None:
        self.db.close()


class ResolveVersionChainTests(_SqliteDbTestCase):
    def test_fresh_group_backfills_and_returns_next_version(self) -> None:
        source = Video(
            project_id=self.project.id,
            name="Legacy Upload",
            version=1,
            version_group_id=None,
            file_path="legacy.mp4",
            uploader_id=self.user.id,
        )
        self.db.add(source)
        self.db.commit()

        version_group_id, version = resolve_version_chain(self.db, source)

        self.assertTrue(version_group_id)
        self.assertEqual(source.version_group_id, version_group_id)
        # The source itself (now backfilled into the group as v1) counts,
        # so the next version is 2 — matches the upload route's behavior.
        self.assertEqual(version, 2)

    def test_existing_group_returns_max_plus_one(self) -> None:
        v1 = Video(
            project_id=self.project.id,
            name="Hero Cut v1",
            version=1,
            version_group_id="grp-hero",
            file_path="v1.mp4",
            uploader_id=self.user.id,
        )
        v2 = Video(
            project_id=self.project.id,
            name="Hero Cut v2",
            version=2,
            version_group_id="grp-hero",
            file_path="v2.mp4",
            uploader_id=self.user.id,
        )
        self.db.add_all([v1, v2])
        self.db.commit()

        version_group_id, version = resolve_version_chain(self.db, v2)

        self.assertEqual(version_group_id, "grp-hero")
        self.assertEqual(version, 3)

    def test_does_not_leak_across_projects(self) -> None:
        other_project = Project(name="Other", creator_id=self.user.id, workspace_id=1)
        self.db.add(other_project)
        self.db.flush()

        source = Video(
            project_id=self.project.id,
            name="Source",
            version=1,
            version_group_id="grp-shared-id",
            file_path="source.mp4",
            uploader_id=self.user.id,
        )
        # Same version_group_id string, but different project — must not count.
        other = Video(
            project_id=other_project.id,
            name="Other project video",
            version=5,
            version_group_id="grp-shared-id",
            file_path="other.mp4",
            uploader_id=self.user.id,
        )
        self.db.add_all([source, other])
        self.db.commit()

        _, version = resolve_version_chain(self.db, source)
        self.assertEqual(version, 2)


class RegisterVideoVersionTests(_SqliteDbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.folder = Folder(project_id=self.project.id, name="B-roll", created_by=self.user.id)
        self.db.add(self.folder)
        self.db.flush()

        self.source = Video(
            project_id=self.project.id,
            folder_id=self.folder.id,
            name="Interview",
            version=1,
            version_group_id="grp-interview",
            file_path="https://cdn.example.test/source.mp4",
            uploader_id=self.user.id,
        )
        self.db.add(self.source)
        self.db.commit()

    def test_creates_video_in_same_project_and_folder_with_next_version(self) -> None:
        with mock.patch(
            "app.jobs.queue.enqueue_video_thumbnail_job", return_value=True
        ) as enqueue_mock, mock.patch(
            "app.services.video_versions.log_activity"
        ) as log_activity_mock:
            new_video = register_video_version(
                self.db,
                self.source,
                name="Interview (edited)",
                file_path="https://cdn.example.test/edited.mp4",
                size_bytes=12345,
            )
            self.db.commit()

        self.assertIsNotNone(new_video.id)
        self.assertEqual(new_video.project_id, self.source.project_id)
        self.assertEqual(new_video.folder_id, self.source.folder_id)
        self.assertEqual(new_video.version_group_id, "grp-interview")
        self.assertEqual(new_video.version, 2)
        self.assertEqual(new_video.name, "Interview (edited)")
        self.assertEqual(new_video.file_path, "https://cdn.example.test/edited.mp4")
        self.assertEqual(new_video.size_bytes, 12345)
        self.assertEqual(new_video.uploader_id, self.source.uploader_id)
        enqueue_mock.assert_called_once_with(new_video.id)

        # An activity entry is logged for the newly-registered version,
        # same pattern as _finalize_project_video's upload-time log_activity call.
        log_activity_mock.assert_called_once()
        _, log_kwargs = log_activity_mock.call_args
        self.assertEqual(log_kwargs["user_id"], self.source.uploader_id)
        self.assertEqual(log_kwargs["project_id"], self.source.project_id)
        self.assertEqual(log_kwargs["action"], "video_version_registered")
        self.assertEqual(log_kwargs["meta"]["video_id"], new_video.id)
        self.assertEqual(log_kwargs["meta"]["source_video_id"], self.source.id)
        self.assertEqual(log_kwargs["meta"]["version"], 2)

    def test_no_transcription_row_seeded(self) -> None:
        with mock.patch("app.jobs.queue.enqueue_video_thumbnail_job", return_value=True):
            new_video = register_video_version(
                self.db,
                self.source,
                name="Interview (edited)",
                file_path="https://cdn.example.test/edited.mp4",
            )
            self.db.commit()

        tr = (
            self.db.query(VideoTranscription)
            .filter(VideoTranscription.video_id == new_video.id)
            .first()
        )
        self.assertIsNone(tr)

    def test_thumbnail_job_skipped_when_thumbnail_url_provided(self) -> None:
        with mock.patch("app.jobs.queue.enqueue_video_thumbnail_job", return_value=True) as enqueue_mock:
            new_video = register_video_version(
                self.db,
                self.source,
                name="Interview (edited)",
                file_path="https://cdn.example.test/edited.mp4",
                thumbnail_url="https://cdn.example.test/edited-thumb.jpg",
            )
            self.db.commit()

        self.assertEqual(new_video.thumbnail_url, "https://cdn.example.test/edited-thumb.jpg")
        enqueue_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
