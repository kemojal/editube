"""Redis Queue helpers for background transcription."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def enqueue_transcription_job(video_id: int) -> bool:
    """
    Enqueue transcribe_video for this video. Returns True if a job was queued.
    If REDIS_URL is unset or enqueue fails, returns False (row stays pending).
    """
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        logger.warning("REDIS_URL not set; transcription job not enqueued for video %s", video_id)
        return False
    try:
        from app.jobs.transcription import transcribe_video
        from redis import Redis
        from rq import Queue

        conn = Redis.from_url(url)
        q = Queue("default", connection=conn, default_timeout=3600)
        q.enqueue(
            transcribe_video,
            video_id,
            job_timeout=3600,
        )
        return True
    except Exception as e:
        logger.exception("Failed to enqueue transcription for video %s: %s", video_id, e)
        return False


def enqueue_mention_email_job(
    recipient_user_id: int,
    actor_name: str,
    project_name: str | None,
    video_name: str | None,
    comment_text: str,
    comment_url: str,
) -> bool:
    """Enqueue async mention email send. Returns True if queued."""
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        logger.warning(
            "REDIS_URL not set; mention email job not enqueued for user %s",
            recipient_user_id,
        )
        return False
    try:
        from app.jobs.mention_email import send_mention_email_job
        from redis import Redis
        from rq import Queue

        conn = Redis.from_url(url)
        q = Queue("default", connection=conn, default_timeout=3600)
        q.enqueue(
            send_mention_email_job,
            recipient_user_id,
            actor_name,
            project_name,
            video_name,
            comment_text,
            comment_url,
            job_timeout=300,
        )
        return True
    except Exception as e:
        logger.exception(
            "Failed to enqueue mention email for user %s: %s",
            recipient_user_id,
            e,
        )
        return False
