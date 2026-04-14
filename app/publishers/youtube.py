from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.jobs.queue import enqueue_youtube_publish_job

from app.publishers.base import Publisher

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.db.models import VideoPublication

logger = logging.getLogger(__name__)


class YoutubePublisher(Publisher):
    def start_publish(self, db: "Session", publication: "VideoPublication") -> None:
        publication.status = "queued"
        publication.error_message = None
        db.add(publication)
        db.flush()
        if not enqueue_youtube_publish_job(publication.id):
            publication.status = "failed"
            publication.error_message = (
                "Could not queue YouTube publish (set REDIS_URL and run an RQ worker)."
            )
            logger.error("enqueue_youtube_publish_job failed for publication %s", publication.id)
