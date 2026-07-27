"""Redis Queue helpers for background transcription."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def enqueue_rough_cut_export_job(ai_result_id: int, *, register_as_version: bool = False) -> str | None:
    """Enqueue FFmpeg concat/upload for AiResult rough_cut_export row. Returns RQ job id or None."""
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        logger.warning("REDIS_URL not set; rough-cut export not enqueued for ai_result %s", ai_result_id)
        return None
    try:
        from app.jobs.rough_cut_export import rough_cut_export_job
        from redis import Redis
        from rq import Queue

        timeout_sec = max(3600, int(os.environ.get("ROUGH_CUT_EXPORT_TIMEOUT_SEC", "14400") or "14400"))
        conn = Redis.from_url(url)
        q = Queue("default", connection=conn, default_timeout=timeout_sec)
        job = q.enqueue(
            rough_cut_export_job,
            ai_result_id,
            register_as_version=register_as_version,
            job_timeout=timeout_sec,
        )
        return job.get_id() if job else None
    except Exception as e:
        logger.exception("Failed to enqueue rough-cut export ai_result=%s: %s", ai_result_id, e)
        return None


def enqueue_rough_cut_effect_job(ai_result_id: int) -> str | None:
    """Enqueue rough-cut clip effect processing. Returns RQ job id or None."""
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        logger.warning("REDIS_URL not set; rough-cut effect not enqueued for ai_result %s", ai_result_id)
        return None
    try:
        from app.jobs.rough_cut_effect import rough_cut_effect_job
        from redis import Redis
        from rq import Queue

        timeout_sec = max(900, int(os.environ.get("ROUGH_CUT_EFFECT_TIMEOUT_SEC", "7200") or "7200"))
        conn = Redis.from_url(url)
        q = Queue("default", connection=conn, default_timeout=timeout_sec)
        job = q.enqueue(
            rough_cut_effect_job,
            ai_result_id,
            job_timeout=timeout_sec,
        )
        return job.get_id() if job else None
    except Exception as e:
        logger.exception("Failed to enqueue rough-cut effect ai_result=%s: %s", ai_result_id, e)
        return None


def enqueue_transcription_job(video_id: int, language: str | None = None) -> bool:
    """
    Enqueue transcribe_video for this video. Returns True if a job was queued.
    If REDIS_URL is unset or enqueue fails, returns False (row stays pending).

    `language` is an optional ISO 639-1 code (e.g. "en"); None means auto-detect
    (the row's own `language` column is also consulted by the worker).
    """
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        logger.warning("REDIS_URL not set; transcription job not enqueued for video %s", video_id)
        return False
    try:
        from app.jobs.transcription import transcribe_video
        from redis import Redis
        from rq import Queue

        timeout_sec = int(os.environ.get("TRANSCRIPTION_JOB_TIMEOUT_SEC", "14400") or "14400")
        timeout_sec = max(600, min(timeout_sec, 86400))

        conn = Redis.from_url(url)
        q = Queue("default", connection=conn, default_timeout=timeout_sec)
        q.enqueue(
            transcribe_video,
            video_id,
            language,
            job_timeout=timeout_sec,
        )
        return True
    except Exception as e:
        logger.exception("Failed to enqueue transcription for video %s: %s", video_id, e)
        return False


def enqueue_video_thumbnail_job(video_id: int) -> bool:
    """Enqueue ffmpeg poster-frame extraction for a freshly uploaded video.

    Best-effort: replaces Cloudinary's on-the-fly URL thumbnails for R2/local
    backends. Returns True if a job was queued.
    """
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        logger.warning("REDIS_URL not set; thumbnail job not enqueued for video %s", video_id)
        return False
    try:
        from redis import Redis
        from rq import Queue

        conn = Redis.from_url(url)
        q = Queue("default", connection=conn, default_timeout=900)
        q.enqueue(
            "app.jobs.thumbnail.video_thumbnail_job",
            video_id,
            job_timeout=600,
        )
        return True
    except Exception as e:
        logger.exception("Failed to enqueue thumbnail for video %s: %s", video_id, e)
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


def enqueue_proxy_generation_job(proxy_id: int) -> bool:
    """Enqueue FFmpeg proxy transcoding for a VideoProxy row."""
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        logger.warning("REDIS_URL not set; proxy generation not enqueued for proxy %s", proxy_id)
        return False
    try:
        from redis import Redis
        from rq import Queue

        conn = Redis.from_url(url)
        q = Queue("default", connection=conn, default_timeout=7200)
        q.enqueue(
            "app.jobs.proxy_generation.proxy_generation_job",
            proxy_id,
            job_timeout=7200,
        )
        return True
    except Exception as e:
        logger.exception("Failed to enqueue proxy generation for proxy %s: %s", proxy_id, e)
        return False


def enqueue_clip_render_job(clip_id: int) -> str | None:
    """Enqueue clip render. Returns the RQ job id if queued, else None."""
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        logger.warning("REDIS_URL not set; clip render not enqueued for clip %s", clip_id)
        return None
    try:
        from app.jobs.clip_render import clip_render_job
        from redis import Redis
        from rq import Queue

        conn = Redis.from_url(url)
        q = Queue("default", connection=conn, default_timeout=7200)
        job = q.enqueue(clip_render_job, clip_id, job_timeout=7200)
        return job.id
    except Exception as e:
        logger.exception("Failed to enqueue clip render for clip %s: %s", clip_id, e)
        return None


def enqueue_drive_import_job(import_id: int) -> str | None:
    """Enqueue a Google Drive file pull into our storage. Returns RQ job id or None."""
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        logger.warning("REDIS_URL not set; drive import not enqueued for %s", import_id)
        return None
    try:
        from redis import Redis
        from rq import Queue

        timeout_sec = max(600, int(os.environ.get("DRIVE_IMPORT_JOB_TIMEOUT_SEC", "3600") or "3600"))
        conn = Redis.from_url(url)
        q = Queue("default", connection=conn, default_timeout=timeout_sec)
        job = q.enqueue(
            "app.jobs.drive_import.drive_import_job",
            import_id,
            job_timeout=timeout_sec,
        )
        return job.get_id() if job else None
    except Exception as e:
        logger.exception("Failed to enqueue drive import %s: %s", import_id, e)
        return None


def enqueue_watch_folder_sync_job(config_id: int) -> bool:
    """Enqueue watch folder sync processing."""
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        logger.warning("REDIS_URL not set; watch folder sync not enqueued for config %s", config_id)
        return False
    try:
        from redis import Redis
        from rq import Queue

        conn = Redis.from_url(url)
        q = Queue("default", connection=conn, default_timeout=1800)
        q.enqueue(
            "app.jobs.watch_folder_sync.watch_folder_sync_job",
            config_id,
            job_timeout=1800,
        )
        return True
    except Exception as e:
        logger.exception("Failed to enqueue watch folder sync for config %s: %s", config_id, e)
        return False


# --- AI UGC ---------------------------------------------------------------


def enqueue_ugc_product_import_job(product_id: int) -> str | None:
    """Enqueue product-URL extraction. Returns RQ job id or None."""
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        logger.warning("REDIS_URL not set; ugc product import not enqueued for %s", product_id)
        return None
    try:
        from redis import Redis
        from rq import Queue

        conn = Redis.from_url(url)
        q = Queue("default", connection=conn, default_timeout=600)
        job = q.enqueue("app.jobs.ugc_product_import.ugc_product_import_job", product_id, job_timeout=600)
        return job.id if job else None
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to enqueue ugc product import %s: %s", product_id, e)
        return None


def enqueue_ugc_brief_generate_job(product_id: int) -> str | None:
    """Enqueue brief + hooks/scripts/CTAs generation. Returns RQ job id or None."""
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        logger.warning("REDIS_URL not set; ugc brief gen not enqueued for product %s", product_id)
        return None
    try:
        from redis import Redis
        from rq import Queue

        conn = Redis.from_url(url)
        q = Queue("default", connection=conn, default_timeout=900)
        job = q.enqueue("app.jobs.ugc_brief_generate.ugc_brief_generate_job", product_id, job_timeout=900)
        return job.id if job else None
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to enqueue ugc brief gen for product %s: %s", product_id, e)
        return None


def enqueue_ugc_render_job(variation_id: int) -> str | None:
    """Enqueue UGC variation render. Returns RQ job id or None."""
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        logger.warning("REDIS_URL not set; ugc render not enqueued for variation %s", variation_id)
        return None
    try:
        from redis import Redis
        from rq import Queue

        timeout_sec = max(600, int(os.environ.get("UGC_RENDER_JOB_TIMEOUT_SEC", "7200") or "7200"))
        conn = Redis.from_url(url)
        q = Queue("default", connection=conn, default_timeout=timeout_sec)
        job = q.enqueue("app.jobs.ugc_render.ugc_render_job", variation_id, job_timeout=timeout_sec)
        return job.id if job else None
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to enqueue ugc render for variation %s: %s", variation_id, e)
        return None


def enqueue_ugc_variation_generate_job(
    campaign_id: int, count: int, dimensions: dict | None = None
) -> str | None:
    """Enqueue background variation fan-out. Returns RQ job id or None."""
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        logger.warning("REDIS_URL not set; ugc variation gen not enqueued for campaign %s", campaign_id)
        return None
    try:
        from redis import Redis
        from rq import Queue

        conn = Redis.from_url(url)
        q = Queue("default", connection=conn, default_timeout=1800)
        job = q.enqueue(
            "app.jobs.ugc_variation_generate.ugc_variation_generate_job",
            campaign_id,
            count,
            dimensions,
            job_timeout=1800,
        )
        return job.id if job else None
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to enqueue ugc variation gen for campaign %s: %s", campaign_id, e)
        return None
