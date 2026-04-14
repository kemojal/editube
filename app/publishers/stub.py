from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.publishers.base import Publisher

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.db.models import VideoPublication


class StubPublisher(Publisher):
    """Non-YouTube platforms: placeholder until TikTok/IG APIs are integrated."""

    def start_publish(self, db: "Session", publication: "VideoPublication") -> None:
        publication.status = "published"
        publication.published_at = datetime.now(timezone.utc)
        publication.external_url = publication.external_url or (
            f"https://example.invalid/{publication.platform}/{publication.id}"
        )
