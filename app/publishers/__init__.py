from app.publishers.base import Publisher
from app.publishers.stub import StubPublisher
from app.publishers.youtube import YoutubePublisher


def get_publisher(platform: str) -> Publisher:
    if (platform or "").lower() == "youtube":
        return YoutubePublisher()
    return StubPublisher()


__all__ = ["Publisher", "StubPublisher", "YoutubePublisher", "get_publisher"]
