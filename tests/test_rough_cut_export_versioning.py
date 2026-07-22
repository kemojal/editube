"""Task D6: "export creates a version" — rough-cut export can register its
rendered output as the NEXT VERSION of the source video.

Covers:
- POST /videos/{id}/ai/rough-cut-export threads `register_as_version` from
  the request body into `enqueue_rough_cut_export_job` (flag default off,
  back-compat).
- `rough_cut_export_job` (ffmpeg/upload mocked): flag off -> no Video
  created, only downloadUrl written; flag on -> a new Video row is created
  in the source's version chain and `versionVideoId` is written alongside
  downloadUrl; registration failure does not fail the export (downloadUrl
  still written, status still "completed").
- WAV exports never register a version even if the flag is set (audio-only
  download, not a playable video).
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import ActivityFeed, AiResult, Project, User, Video, VideoTranscription
from app.jobs import rough_cut_export as rce


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(type_, compiler, **kw):
    # Tests run against in-memory SQLite; JSONB columns degrade to plain JSON.
    return "JSON"


class _SqliteDbTestCase(unittest.TestCase):
    tables = [
        User.__table__,
        Project.__table__,
        Video.__table__,
        VideoTranscription.__table__,
        AiResult.__table__,
        # register_video_version (called by the job when register_as_version
        # is set) logs a real activity entry now — the table must exist.
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


class StartRoughCutExportFlagThreadingTests(_SqliteDbTestCase):
    """Route-level: register_as_version from the request body reaches
    enqueue_rough_cut_export_job, without needing Redis/RQ."""

    def setUp(self) -> None:
        super().setUp()
        self.video = Video(
            project_id=self.project.id,
            name="Source",
            version=1,
            file_path="https://example.test/source.mp4",
            uploader_id=self.user.id,
        )
        self.db.add(self.video)
        self.db.commit()

    def _call(self, register_as_version: bool | None):
        from app.api.routes import ai as ai_routes

        body_kwargs = {"format": "mp4", "keepRanges": [{"start": 0, "end": 1}], "exportSettings": {}}
        if register_as_version is not None:
            body_kwargs["register_as_version"] = register_as_version
        body = ai_routes.RoughCutExportBody(**body_kwargs)

        with mock.patch("app.jobs.queue.enqueue_rough_cut_export_job", return_value="rq-job-1") as enqueue_mock:
            result = ai_routes.start_rough_cut_export(
                video_id=self.video.id, body=body, db=self.db, current_user=self.user
            )
        return result, enqueue_mock

    def test_flag_omitted_defaults_false(self) -> None:
        result, enqueue_mock = self._call(None)
        enqueue_mock.assert_called_once()
        _, kwargs = enqueue_mock.call_args
        self.assertEqual(kwargs.get("register_as_version"), False)
        self.assertEqual(result["status"], "queued")

    def test_flag_true_is_threaded_through(self) -> None:
        result, enqueue_mock = self._call(True)
        enqueue_mock.assert_called_once()
        _, kwargs = enqueue_mock.call_args
        self.assertEqual(kwargs.get("register_as_version"), True)
        self.assertEqual(result["status"], "queued")


class RoughCutExportJobVersioningTests(_SqliteDbTestCase):
    """Job-level: ffmpeg + upload mocked; only the registration branch is
    under test."""

    def setUp(self) -> None:
        super().setUp()
        self.video = Video(
            project_id=self.project.id,
            name="Interview",
            version=1,
            version_group_id="grp-1",
            file_path="/tmp/source.mp4",
            uploader_id=self.user.id,
        )
        self.db.add(self.video)
        self.db.commit()

        self.ai_result = AiResult(
            video_id=self.video.id,
            result_type="rough_cut_export",
            status="queued",
            result_data={
                "format": "mp4",
                "keepRanges": [{"start": 0, "end": 2}],
                "exportSettings": {},
            },
        )
        self.db.add(self.ai_result)
        self.db.commit()

        # The job calls SessionLocal() once for its own db handle — reuse
        # this test's session instead of opening a second sqlite connection
        # (in-memory sqlite connections don't share state).
        patcher = mock.patch("app.jobs.rough_cut_export.SessionLocal", return_value=self.db)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The job's `finally` closes its db handle; don't let that tear down
        # the session out from under the rest of the test / tearDown.
        close_patcher = mock.patch.object(self.db, "close", lambda: None)
        close_patcher.start()
        self.addCleanup(close_patcher.stop)

        for target, value in (
            ("app.jobs.rough_cut_export._ffprobe_has_video", True),
            ("app.jobs.rough_cut_export._ffprobe_avg_frame_rate", "30/1"),
            ("app.jobs.rough_cut_export.cloudinary_credentials_configured", True),
        ):
            p = mock.patch(target, return_value=value)
            p.start()
            self.addCleanup(p.stop)

        def _fake_run_ffmpeg(args):
            # Every _run_ffmpeg call in this job ends with the output path;
            # drop a tiny placeholder file so later .stat() calls succeed.
            Path(args[-1]).write_bytes(b"0" * 128)

        run_ffmpeg_patcher = mock.patch(
            "app.jobs.rough_cut_export._run_ffmpeg", side_effect=_fake_run_ffmpeg
        )
        run_ffmpeg_patcher.start()
        self.addCleanup(run_ffmpeg_patcher.stop)

        self.upload_mock = mock.patch(
            "app.jobs.rough_cut_export.upload_local_path_to_cloudinary",
            return_value="https://cdn.example.test/exports/out.mp4",
        )
        self.upload_mock.start()
        self.addCleanup(self.upload_mock.stop)

        # register_video_version best-effort enqueues a thumbnail job; stub
        # it out so the test never depends on a real Redis being reachable.
        thumb_patcher = mock.patch("app.jobs.queue.enqueue_video_thumbnail_job", return_value=True)
        thumb_patcher.start()
        self.addCleanup(thumb_patcher.stop)

    def _video_count(self) -> int:
        return self.db.query(Video).count()

    def test_flag_off_no_video_created(self) -> None:
        rce.rough_cut_export_job(self.ai_result.id)

        self.db.refresh(self.ai_result)
        self.assertEqual(self.ai_result.status, "completed")
        self.assertEqual(self.ai_result.result_data.get("downloadUrl"), "https://cdn.example.test/exports/out.mp4")
        self.assertNotIn("versionVideoId", self.ai_result.result_data)
        self.assertEqual(self._video_count(), 1)

    def test_flag_on_creates_version_with_correct_group_and_number(self) -> None:
        rce.rough_cut_export_job(self.ai_result.id, register_as_version=True)

        self.db.refresh(self.ai_result)
        self.assertEqual(self.ai_result.status, "completed")
        self.assertEqual(self._video_count(), 2)

        version_id = self.ai_result.result_data.get("versionVideoId")
        self.assertIsNotNone(version_id)
        new_video = self.db.query(Video).filter(Video.id == version_id).first()
        self.assertIsNotNone(new_video)
        self.assertEqual(new_video.version_group_id, "grp-1")
        self.assertEqual(new_video.version, 2)
        self.assertEqual(new_video.name, "Interview (edited)")
        self.assertEqual(new_video.file_path, "https://cdn.example.test/exports/out.mp4")
        self.assertEqual(new_video.project_id, self.video.project_id)

    def test_registration_failure_does_not_fail_export(self) -> None:
        with mock.patch(
            "app.services.video_versions.register_video_version",
            side_effect=RuntimeError("boom"),
        ):
            rce.rough_cut_export_job(self.ai_result.id, register_as_version=True)

        self.db.refresh(self.ai_result)
        self.assertEqual(self.ai_result.status, "completed")
        self.assertEqual(self.ai_result.result_data.get("downloadUrl"), "https://cdn.example.test/exports/out.mp4")
        self.assertNotIn("versionVideoId", self.ai_result.result_data)
        # Only the original source video exists — the failed registration
        # left no partial row behind.
        self.assertEqual(self._video_count(), 1)

    def test_wav_export_never_registers_even_if_flag_set(self) -> None:
        self.ai_result.result_data = {
            "format": "wav",
            "keepRanges": [{"start": 0, "end": 2}],
            "exportSettings": {},
        }
        self.db.commit()

        rce.rough_cut_export_job(self.ai_result.id, register_as_version=True)

        self.db.refresh(self.ai_result)
        self.assertEqual(self.ai_result.status, "completed")
        self.assertEqual(self.ai_result.result_data.get("format"), "wav")
        self.assertNotIn("versionVideoId", self.ai_result.result_data)
        self.assertEqual(self._video_count(), 1)


if __name__ == "__main__":
    unittest.main()
