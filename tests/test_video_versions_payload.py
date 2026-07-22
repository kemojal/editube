import unittest

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.api.video_payload import video_versions_payload
from app.db.database import Base
from app.db.models import Comment, Project, User, Video


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(type_, compiler, **kw):
    # Tests run against in-memory SQLite; JSONB columns degrade to plain JSON.
    return "JSON"


class VideoVersionsPayloadTests(unittest.TestCase):
    """versions[] must be the video's own version chain, not every project video."""

    def setUp(self) -> None:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(
            engine,
            tables=[
                User.__table__,
                Project.__table__,
                Video.__table__,
                Comment.__table__,
            ],
        )
        self.db = sessionmaker(bind=engine)()

        self.uploader = User(email="editor@example.com", name="Edna Editor", role="creator")
        self.db.add(self.uploader)
        self.db.flush()

        self.project = Project(
            name="Launch Campaign",
            creator_id=self.uploader.id,
            workspace_id=1,
        )
        self.db.add(self.project)
        self.db.flush()

        def add_video(name: str, version: int, group: str | None) -> Video:
            video = Video(
                project_id=self.project.id,
                name=name,
                version=version,
                version_group_id=group,
                file_path=f"{name}.mp4",
                uploader_id=self.uploader.id,
            )
            self.db.add(video)
            self.db.flush()
            return video

        # One chain of 2 versions + 2 separate single-version chains, all in one project.
        self.chain_v1 = add_video("Hero Cut v1", 1, "grp-hero")
        self.chain_v2 = add_video("Hero Cut v2", 2, "grp-hero")
        self.other_a = add_video("Teaser", 1, "grp-teaser")
        self.other_b = add_video("BTS Reel", 1, "grp-bts")

        for video, count in ((self.chain_v1, 1), (self.chain_v2, 2), (self.other_a, 5)):
            for i in range(count):
                self.db.add(Comment(video_id=video.id, user_id=self.uploader.id, text=f"note {i}"))
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    def test_v2_returns_only_its_own_chain_newest_first(self) -> None:
        payload = video_versions_payload(self.db, self.chain_v2)

        self.assertEqual([entry["id"] for entry in payload], [self.chain_v2.id, self.chain_v1.id])
        self.assertEqual([entry["version"] for entry in payload], [2, 1])
        # The other two deliverables in the project must not leak in as "versions".
        self.assertNotIn(self.other_a.id, {entry["id"] for entry in payload})
        self.assertNotIn(self.other_b.id, {entry["id"] for entry in payload})

    def test_single_version_chain_returns_only_itself(self) -> None:
        payload = video_versions_payload(self.db, self.other_a)
        self.assertEqual([entry["id"] for entry in payload], [self.other_a.id])

    def test_null_group_video_is_its_own_chain(self) -> None:
        orphan = Video(
            project_id=self.project.id,
            name="Legacy Upload",
            version=1,
            version_group_id=None,
            file_path="legacy.mp4",
            uploader_id=self.uploader.id,
        )
        self.db.add(orphan)
        self.db.commit()

        payload = video_versions_payload(self.db, orphan)
        self.assertEqual([entry["id"] for entry in payload], [orphan.id])

    def test_payload_includes_comment_count_and_uploader_name(self) -> None:
        payload = video_versions_payload(self.db, self.chain_v2)

        by_id = {entry["id"]: entry for entry in payload}
        self.assertEqual(by_id[self.chain_v2.id]["comment_count"], 2)
        self.assertEqual(by_id[self.chain_v1.id]["comment_count"], 1)
        for entry in payload:
            self.assertEqual(entry["uploader_name"], "Edna Editor")
            self.assertIn("thumbnail_url", entry)
            self.assertIn("created_at", entry)
            self.assertIn("name", entry)


if __name__ == "__main__":
    unittest.main()
