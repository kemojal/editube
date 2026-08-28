"""RQ job for the multimodal AI review.

The review samples ~10 frames with ffmpeg (each an HTTP range-seek when the
media lives in object storage) before it even calls the model, which does not
fit inside a request. The route enqueues this and the player polls the
``review`` AiResult row until it leaves ``processing``.
"""
from __future__ import annotations

import logging
from typing import Any

from app.db.database import SessionLocal
from app.db.models import AiResult, Comment, Video, VideoTranscription
from app.services.auto_edit import AutoEditOptions
from app.services.video_review import build_review, empty_review

logger = logging.getLogger(__name__)


def _write_result(
    db, video_id: int, data: dict[str, Any], *, status: str, error: str | None = None
) -> None:
    row = (
        db.query(AiResult)
        .filter(AiResult.video_id == video_id, AiResult.result_type == "review")
        .first()
    )
    if row is None:
        row = AiResult(video_id=video_id, result_type="review")
        db.add(row)
    row.status = status
    row.error_message = error
    row.result_data = data
    db.commit()


def ai_review_job(video_id: int, options: dict[str, Any] | None = None) -> None:
    """Build the review for ``video_id`` and store it on the ``review`` row."""
    db = SessionLocal()
    try:
        video = db.query(Video).filter(Video.id == video_id).first()
        if not video:
            raise RuntimeError(f"Video {video_id} was removed before AI review started")

        transcription = (
            db.query(VideoTranscription)
            .filter(VideoTranscription.video_id == video_id)
            .first()
        )
        segments = list(transcription.segments) if transcription and transcription.segments else []
        if not segments:
            _write_result(
                db,
                video_id,
                empty_review("Transcribe the video first to run an AI review."),
                status="completed",
            )
            return

        comments = (
            db.query(Comment)
            .filter(Comment.video_id == video_id)
            .order_by(Comment.timecode.asc())
            .limit(60)
            .all()
        )

        payload = build_review(
            video_id=video_id,
            duration=float(getattr(video, "duration", 0) or 0),
            media_src=str(getattr(video, "file_path", "") or ""),
            segments=segments,
            comments=[{"timecode": c.timecode, "text": c.text} for c in comments],
            options=AutoEditOptions(**(options or {})),
        )
        _write_result(db, video_id, payload, status="completed")
    except Exception as exc:  # noqa: BLE001 — surface the failure in the UI
        logger.exception("ai_review_job failed for video %s", video_id)
        try:
            _write_result(
                db,
                video_id,
                empty_review("The AI review could not be completed."),
                status="failed",
                error=str(exc)[:500],
            )
        except Exception:
            logger.exception("ai_review_job could not record failure for video %s", video_id)
        raise
    finally:
        db.close()
