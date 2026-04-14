"""Create or reset a video_transcriptions row and enqueue the RQ job."""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.db.models import VideoTranscription

logger = logging.getLogger(__name__)


def prepare_and_enqueue_transcription(db: Session, video_id: int) -> None:
    """
    Ensure a transcription row exists, then enqueue unless already queued/processing.
    Clears segments and error_message when re-starting.
    Raises HTTPException 409 if a job is already queued or running.
    """
    vt = db.query(VideoTranscription).filter(VideoTranscription.video_id == video_id).first()
    if vt is None:
        vt = VideoTranscription(video_id=video_id, status="pending")
        db.add(vt)
        db.flush()

    if vt.status in ("queued", "processing"):
        raise HTTPException(
            status_code=409,
            detail="Transcription is already queued or in progress.",
        )

    vt.status = "pending"
    vt.error_message = None
    vt.segments = None
    vt.model_name = None
    db.commit()

    try:
        from app.jobs.queue import enqueue_transcription_job

        if enqueue_transcription_job(video_id):
            row = (
                db.query(VideoTranscription)
                .filter(VideoTranscription.video_id == video_id)
                .first()
            )
            if row:
                row.status = "queued"
                db.commit()
    except Exception as e:
        logger.warning("Transcription job not enqueued for video %s: %s", video_id, e)
