"""
RQ job: render a Clip to MP4 via ffmpeg and update its status/progress.
Run worker from editube/ with:
    rq worker -u "$REDIS_URL" default
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.db.models import Clip
from app.services.clip_renderer import render_clip

logger = logging.getLogger(__name__)


def _set_progress(db: Session, clip_id: int, progress: int, status: str | None = None) -> None:
    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if clip is None:
        return
    clip.render_progress = max(0, min(100, int(progress)))
    if status:
        clip.status = status
    db.commit()


def clip_render_job(clip_id: int) -> None:
    db: Session = SessionLocal()
    try:
        clip = db.query(Clip).filter(Clip.id == clip_id).first()
        if clip is None:
            raise RuntimeError(f"Clip {clip_id} was removed before rendering started")

        clip.status = "rendering"
        clip.render_progress = 0
        clip.render_error = None
        db.commit()

        def on_progress(p: int) -> None:
            _set_progress(db, clip_id, p, status="rendering")

        render_clip(db, clip_id, on_progress=on_progress)

        clip = db.query(Clip).filter(Clip.id == clip_id).first()
        if clip is not None:
            clip.status = "ready"
            clip.render_progress = 100
            clip.render_error = None
            clip.completed_at = datetime.utcnow()
            db.commit()
        logger.info("clip_render_job: clip %s rendered successfully", clip_id)
    except Exception as e:  # noqa: BLE001
        logger.exception("clip_render_job failed for clip %s", clip_id)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        clip = db.query(Clip).filter(Clip.id == clip_id).first()
        if clip is not None:
            clip.status = "failed"
            clip.render_error = str(e)[:4000]
            db.commit()
        raise
    finally:
        db.close()
