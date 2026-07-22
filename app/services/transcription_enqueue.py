"""Create or reset a video_transcriptions row and enqueue the RQ job."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.db.models import VideoTranscription
from app.utils.language import normalize_language

logger = logging.getLogger(__name__)


def prepare_and_enqueue_transcription(
    db: Session, video_id: int, *, force: bool = False, language: Optional[str] = None
) -> None:
    """
    Ensure a transcription row exists, then enqueue unless already queued/processing.
    Clears segments and error_message when re-starting.
    Raises HTTPException 409 if a job is already queued or running, unless force=True
    (use when the worker died, RQ timed out, or a signed stream URL expired mid-job).

    `language` is an optional ISO 639-1 code ("auto"/""/None = auto-detect). When
    omitted (None), the row's existing language selection (if any) is left as-is;
    when supplied, it replaces the row's language for this run.
    """
    vt = db.query(VideoTranscription).filter(VideoTranscription.video_id == video_id).first()
    if vt is None:
        vt = VideoTranscription(video_id=video_id, status="pending")
        db.add(vt)
        db.flush()

    if language is not None:
        vt.language = normalize_language(language)

    if vt.status in ("queued", "processing"):
        if not force:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Transcription is already queued or in progress. "
                    "Pass force=true if the job is stuck (worker crash, RQ timeout, expired YouTube URL)."
                ),
            )
        vt.status = "pending"
        vt.error_message = None
        vt.segments = None
        vt.model_name = None
        db.commit()

    vt.status = "pending"
    vt.error_message = None
    vt.segments = None
    vt.model_name = None
    db.commit()

    try:
        from app.jobs.queue import enqueue_transcription_job

        enqueued = enqueue_transcription_job(video_id, language=vt.language)
        row = (
            db.query(VideoTranscription)
            .filter(VideoTranscription.video_id == video_id)
            .first()
        )
        if row:
            if enqueued:
                row.status = "queued"
            else:
                row.status = "failed"
                row.error_message = (
                    "Transcription was not queued. Set REDIS_URL for the API and worker, "
                    "then run an RQ worker on the default queue "
                    '(example: rq worker -u "$REDIS_URL" default or ./scripts/dev_with_worker.sh).'
                )
            db.commit()
    except Exception as e:
        logger.warning("Transcription enqueue error for video %s: %s", video_id, e)
        try:
            db.rollback()
        except Exception:
            pass
        try:
            row = (
                db.query(VideoTranscription)
                .filter(VideoTranscription.video_id == video_id)
                .first()
            )
            if row:
                row.status = "failed"
                row.error_message = (str(e) or "Could not enqueue transcription job")[:4000]
                db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            logger.exception("Could not persist transcription enqueue failure for video %s", video_id)
