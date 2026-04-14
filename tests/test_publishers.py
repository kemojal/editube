import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.publishers import get_publisher
from app.publishers.stub import StubPublisher
from app.publishers.youtube import YoutubePublisher


class GetPublisherTests(unittest.TestCase):
    def test_youtube_returns_youtube_publisher(self) -> None:
        self.assertIsInstance(get_publisher("youtube"), YoutubePublisher)

    def test_other_platform_returns_stub(self) -> None:
        self.assertIsInstance(get_publisher("tiktok"), StubPublisher)


class StubPublisherTests(unittest.TestCase):
    def test_marks_published(self) -> None:
        pub = SimpleNamespace(
            id=9,
            platform="tiktok",
            status="draft",
            published_at=None,
            external_url=None,
        )
        StubPublisher().start_publish(SimpleNamespace(), pub)
        self.assertEqual(pub.status, "published")
        self.assertIsNotNone(pub.published_at)
        self.assertIn("example.invalid", pub.external_url or "")


class YoutubePublisherTests(unittest.TestCase):
    def test_enqueue_failure_sets_failed(self) -> None:
        pub = SimpleNamespace(id=3, platform="youtube", status="draft", error_message=None)
        db = MagicMock()
        with patch("app.publishers.youtube.enqueue_youtube_publish_job", return_value=False):
            YoutubePublisher().start_publish(db, pub)
        self.assertEqual(pub.status, "failed")
        self.assertIn("REDIS_URL", pub.error_message or "")
        db.flush.assert_called_once()

    def test_enqueue_success_sets_queued(self) -> None:
        pub = SimpleNamespace(id=3, platform="youtube", status="draft", error_message="old")
        db = MagicMock()
        with patch("app.publishers.youtube.enqueue_youtube_publish_job", return_value=True):
            YoutubePublisher().start_publish(db, pub)
        self.assertEqual(pub.status, "queued")
        self.assertIsNone(pub.error_message)
        db.flush.assert_called_once()


if __name__ == "__main__":
    unittest.main()
