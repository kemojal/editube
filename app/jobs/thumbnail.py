"""RQ job: generate + store a poster thumbnail for an uploaded video."""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def video_thumbnail_job(video_id: int) -> None:
    from app.db.database import SessionLocal
    from app.db.models import Video
    from app.services.thumbnail import generate_and_store_thumbnail

    db = SessionLocal()
    try:
        video = db.query(Video).filter(Video.id == video_id).first()
        if video is None or video.thumbnail_url:
            return  # gone, or already has a thumbnail
        src = str(video.file_path or "").strip()
        if not src:
            return
        folder = os.environ.get("STORAGE_THUMBNAIL_FOLDER", "thumbnails")
        url = generate_and_store_thumbnail(
            src, folder=folder, public_id=f"video_{video_id}", seek=1.0
        )
        if url:
            video.thumbnail_url = url
            db.commit()
            logger.info("stored thumbnail for video %s", video_id)
    except Exception:  # noqa: BLE001
        logger.exception("video_thumbnail_job failed for video %s", video_id)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
    finally:
        db.close()
