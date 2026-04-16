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


def enqueue_youtube_publish_job(publication_id: int) -> bool:
    """Enqueue YouTube resumable upload for a VideoPublication."""
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        logger.warning("REDIS_URL not set; YouTube publish not enqueued for publication %s", publication_id)
        return False
    try:
        from redis import Redis
        from rq import Queue

        conn = Redis.from_url(url)
        q = Queue("default", connection=conn, default_timeout=7200)
        q.enqueue(
            "app.jobs.youtube_publish.youtube_publish_job",
            publication_id,
            job_timeout=7200,
        )
        return True
    except Exception as e:
        logger.exception("Failed to enqueue YouTube publish for publication %s: %s", publication_id, e)
        return False


def enqueue_aspect_export_job(export_id: int) -> bool:
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        logger.warning("REDIS_URL not set; aspect export not enqueued for %s", export_id)
        return False
    try:
        from redis import Redis
        from rq import Queue

        conn = Redis.from_url(url)
        q = Queue("default", connection=conn, default_timeout=3600)
        q.enqueue(
            "app.jobs.aspect_export.aspect_export_job",
            export_id,
            job_timeout=3600,
        )
        return True
    except Exception as e:
        logger.exception("Failed to enqueue aspect export %s: %s", export_id, e)
        return False


def enqueue_chapter_synthesis_job(video_id: int) -> bool:
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        logger.warning("REDIS_URL not set; chapter synthesis not enqueued for video %s", video_id)
        return False
    try:
        from redis import Redis
        from rq import Queue

        conn = Redis.from_url(url)
        q = Queue("default", connection=conn, default_timeout=900)
        q.enqueue(
            "app.jobs.chapter_synthesis.chapter_synthesis_job",
            video_id,
            job_timeout=900,
        )
        return True
    except Exception as e:
        logger.exception("Failed to enqueue chapter synthesis for video %s: %s", video_id, e)
        return False


def enqueue_multi_format_export_job(export_id: int) -> bool:
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        logger.warning("REDIS_URL not set; multi-format export not enqueued for %s", export_id)
        return False
    try:
        from redis import Redis
        from rq import Queue

        conn = Redis.from_url(url)
        q = Queue("default", connection=conn, default_timeout=7200)
        q.enqueue(
            "app.jobs.multi_format_export.multi_format_export_job",
            export_id,
            job_timeout=7200,
        )
        return True
    except Exception as e:
        logger.exception("Failed to enqueue multi-format export %s: %s", export_id, e)
        return False


def enqueue_delivery_package_job(package_id: int) -> bool:
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        logger.warning("REDIS_URL not set; delivery package job not enqueued for %s", package_id)
        return False
    try:
        from redis import Redis
        from rq import Queue

        conn = Redis.from_url(url)
        q = Queue("default", connection=conn, default_timeout=7200)
        q.enqueue(
            "app.jobs.delivery_package.delivery_package_job",
            package_id,
            job_timeout=7200,
        )
        return True
    except Exception as e:
        logger.exception("Failed to enqueue delivery package job %s: %s", package_id, e)
        return False


def enqueue_archive_cold_storage_job(project_id: int) -> bool:
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        logger.warning("REDIS_URL not set; archive/cold storage job not enqueued for project %s", project_id)
        return False
    try:
        from redis import Redis
        from rq import Queue

        conn = Redis.from_url(url)
        q = Queue("default", connection=conn, default_timeout=1800)
        q.enqueue(
            "app.jobs.archive_cold_storage.archive_cold_storage_job",
            project_id,
            job_timeout=1800,
        )
        return True
    except Exception as e:
        logger.exception("Failed to enqueue archive/cold storage job for project %s: %s", project_id, e)
        return False


def enqueue_mention_digest_all_job() -> bool:
    """Enqueue batch mention digest for users with email_mention_digest daily/weekly."""
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        logger.warning("REDIS_URL not set; mention digest job not enqueued")
        return False
    try:
        from redis import Redis
        from rq import Queue

        conn = Redis.from_url(url)
        q = Queue("default", connection=conn, default_timeout=900)
        q.enqueue(
            "app.jobs.mention_digest.run_digest_for_all_users",
            job_timeout=900,
        )
        return True
    except Exception as e:
        logger.exception("Failed to enqueue mention digest job: %s", e)
        return False


def enqueue_review_link_maintenance_job() -> bool:
    """Enqueue auto-revoke sweep for expired review links."""
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        logger.warning("REDIS_URL not set; review-link maintenance job not enqueued")
        return False
    try:
        from redis import Redis
        from rq import Queue

        conn = Redis.from_url(url)
        q = Queue("default", connection=conn, default_timeout=900)
        q.enqueue(
            "app.jobs.review_links_maintenance.auto_revoke_expired_review_links",
            job_timeout=900,
        )
        return True
    except Exception as e:
        logger.exception("Failed to enqueue review-link maintenance job: %s", e)
        return False


def enqueue_review_forensic_package_job(forensic_asset_id: int) -> bool:
    """Enqueue forensic packaging for a review session asset."""
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        logger.warning(
            "REDIS_URL not set; forensic packaging not enqueued for asset %s",
            forensic_asset_id,
        )
        return False


def enqueue_push_notification_job(user_id: int, notification_id: int) -> bool:
    """Enqueue native push delivery for a notification."""
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        logger.warning(
            "REDIS_URL not set; push notification not enqueued for user %s notification %s",
            user_id,
            notification_id,
        )
        return False
    try:
        from redis import Redis
        from rq import Queue

        conn = Redis.from_url(url)
        q = Queue("default", connection=conn, default_timeout=300)
        q.enqueue(
            "app.jobs.push_notifications.send_push_notification_job",
            user_id,
            notification_id,
            job_timeout=300,
        )
        return True
    except Exception as e:
        logger.exception(
            "Failed to enqueue push notification for user %s notification %s: %s",
            user_id,
            notification_id,
            e,
        )
        return False
    try:
        from redis import Redis
        from rq import Queue

        conn = Redis.from_url(url)
        q = Queue("default", connection=conn, default_timeout=1800)
        q.enqueue(
            "app.jobs.review_forensic.package_forensic_asset_job",
            forensic_asset_id,
            job_timeout=1800,
        )
        return True
    except Exception as e:
        logger.exception(
            "Failed to enqueue forensic packaging for asset %s: %s",
            forensic_asset_id,
            e,
        )
        return False
