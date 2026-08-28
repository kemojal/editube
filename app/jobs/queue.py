"""Redis Queue helpers for background transcription."""

from __future__ import annotations

import logging
import os
import time

from app.services.observability import observed_span
from app.services.job_terminal_state import feature_key_for_job, persisted_job_context
from app.services.product_analytics import emit_after_commit

logger = logging.getLogger(__name__)


def _record_enqueued(  # noqa: ANN001
    job,
    job_type: str,
    resource_id: int | str,
    *,
    feature_key: str | None = None,
) -> None:
    """Record queue publication once, with safe resource ownership context."""

    if job is None:
        return
    context = persisted_job_context(job_type, resource_id)
    properties = {
        "job_id": str(job.id),
        "job_type": job_type,
        "queue": str(getattr(job, "origin", "default"))[:80],
        "resource_id": resource_id,
        "feature_key": feature_key or feature_key_for_job(job_type, context),
        "project_id": context.get("project_id"),
        "video_id": context.get("video_id"),
    }
    emit_after_commit(
        "job_queued",
        user_id=context.get("user_id"),
        workspace_id=context.get("workspace_id"),
        properties={key: value for key, value in properties.items() if value is not None},
        event_id=f"rq:{job.id}:queued",
    )


def enqueue_analytics_privacy_job(limit: int = 20) -> bool:
    """Enqueue provider data-rights processing independently of ingestion."""
    url = os.environ.get("REDIS_URL", "").strip()
    if not url or not (os.environ.get("POSTHOG_PROJECT_ID", "").strip()):
        return False
    try:
        from redis import Redis
        from rq import Queue

        bucket = int(time.time()) // 60
        conn = Redis.from_url(url, socket_connect_timeout=1, socket_timeout=2)
        queue = Queue("default", connection=conn, default_timeout=300)
        with observed_span("queue.publish", "enqueue analytics privacy", queue="default"):
            queue.enqueue(
                "app.jobs.analytics_privacy.process_analytics_data_requests",
                max(1, min(int(limit), 100)),
                job_id=f"analytics-privacy-{bucket}",
                job_timeout=300,
                result_ttl=60,
                failure_ttl=300,
            )
        return True
    except Exception as exc:
        if "already exists" not in str(exc).lower():
            logger.exception("Failed to enqueue analytics privacy processing")
        return False


def enqueue_analytics_delivery_job(batch_size: int = 100) -> bool:
    """Enqueue one deduplicated outbox-delivery sweep on the default queue."""
    url = os.environ.get("REDIS_URL", "").strip()
    if not url or not os.environ.get("POSTHOG_PROJECT_API_KEY", "").strip():
        return False
    try:
        from redis import Redis
        from rq import Queue

        interval = max(
            10, int(os.environ.get("ANALYTICS_DELIVERY_INTERVAL_SECONDS", "30") or "30")
        )
        bucket = int(time.time()) // interval
        conn = Redis.from_url(url, socket_connect_timeout=1, socket_timeout=2)
        queue = Queue("default", connection=conn, default_timeout=120)
        with observed_span("queue.publish", "enqueue analytics delivery", queue="default"):
            queue.enqueue(
                "app.jobs.analytics_delivery.analytics_delivery_job",
                max(1, min(int(batch_size), 500)),
                job_id=f"analytics-delivery-{bucket}",
                job_timeout=120,
                result_ttl=interval,
                failure_ttl=interval,
            )
        return True
    except Exception as exc:
        # A second API replica can win the same time-bucket job id. That is a
        # successful dedupe, not a product error; every other failure is still
        # visible in worker/API observability.
        if "already exists" not in str(exc).lower():
            logger.exception("Failed to enqueue analytics delivery")
        return False


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
        _record_enqueued(job, "rough_cut_export_job", ai_result_id)
        return job.id if job else None
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
        _record_enqueued(job, "rough_cut_effect_job", ai_result_id)
        return job.id if job else None
    except Exception as e:
        logger.exception("Failed to enqueue rough-cut effect ai_result=%s: %s", ai_result_id, e)
        return None


def enqueue_mask_track_job(ai_result_id: int) -> str | None:
    """Enqueue CV mask tracking. Returns RQ job id or None."""
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        logger.warning("REDIS_URL not set; mask track not enqueued for ai_result %s", ai_result_id)
        return None
    try:
        from app.jobs.mask_track import mask_track_job
        from redis import Redis
        from rq import Queue

        timeout_sec = max(900, int(os.environ.get("MASK_TRACK_TIMEOUT_SEC", "3600") or "3600"))
        conn = Redis.from_url(url)
        q = Queue("default", connection=conn, default_timeout=timeout_sec)
        job = q.enqueue(
            mask_track_job,
            ai_result_id,
            job_timeout=timeout_sec,
        )
        _record_enqueued(job, "mask_track_job", ai_result_id)
        return job.id if job else None
    except Exception as e:
        logger.exception("Failed to enqueue mask track ai_result=%s: %s", ai_result_id, e)
        return None


def enqueue_generated_media_job(media_id: int) -> str | None:
    """Enqueue AI media generation. Returns the RQ job id, or None if unqueued.

    Video jobs poll a long-running operation for minutes, so the timeout is
    generous — it must outlast `AI_VIDEO_TIMEOUT_SEC` or the worker would be
    killed mid-poll and leave the row stuck in `running`.
    """
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        logger.warning("REDIS_URL not set; generation not enqueued for media %s", media_id)
        return None
    try:
        from app.jobs.ai_media_generation import generate_media_job
        from redis import Redis
        from rq import Queue

        timeout_sec = max(1200, int(os.environ.get("AI_MEDIA_JOB_TIMEOUT_SEC", "1800") or "1800"))
        conn = Redis.from_url(url)
        q = Queue("default", connection=conn, default_timeout=timeout_sec)
        job = q.enqueue(generate_media_job, media_id, job_timeout=timeout_sec)
        _record_enqueued(job, "generate_media_job", media_id)
        return job.id if job else None
    except Exception as e:
        logger.exception("Failed to enqueue generated media=%s: %s", media_id, e)
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
        job = q.enqueue(
            transcribe_video,
            video_id,
            language,
            job_timeout=timeout_sec,
        )
        _record_enqueued(job, "transcribe_video", video_id)
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


def enqueue_comment_notification_email_job(
    recipient_user_id: int,
    actor_name: str,
    project_name: str | None,
    video_name: str | None,
    comment_text: str,
    comment_url: str,
) -> bool:
    """Enqueue async 'new comment on your work' email. Returns True if queued."""
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        logger.warning(
            "REDIS_URL not set; comment email job not enqueued for user %s",
            recipient_user_id,
        )
        return False
    try:
        from app.jobs.mention_email import send_comment_notification_email_job
        from redis import Redis
        from rq import Queue

        conn = Redis.from_url(url)
        q = Queue("default", connection=conn, default_timeout=3600)
        q.enqueue(
            send_comment_notification_email_job,
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
            "Failed to enqueue comment email for user %s: %s",
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
        job = q.enqueue(
            "app.jobs.youtube_publish.youtube_publish_job",
            publication_id,
            job_timeout=7200,
        )
        _record_enqueued(job, "youtube_publish_job", publication_id)
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
        job = q.enqueue(
            "app.jobs.aspect_export.aspect_export_job",
            export_id,
            job_timeout=3600,
        )
        _record_enqueued(job, "aspect_export_job", export_id)
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
        job = q.enqueue(
            "app.jobs.chapter_synthesis.chapter_synthesis_job",
            video_id,
            job_timeout=900,
        )
        _record_enqueued(job, "chapter_synthesis_job", video_id)
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
        job = q.enqueue(
            "app.jobs.multi_format_export.multi_format_export_job",
            export_id,
            job_timeout=7200,
        )
        _record_enqueued(job, "multi_format_export_job", export_id)
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
        job = q.enqueue(
            "app.jobs.delivery_package.delivery_package_job",
            package_id,
            job_timeout=7200,
        )
        _record_enqueued(job, "delivery_package_job", package_id)
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
        job = q.enqueue(
            "app.jobs.proxy_generation.proxy_generation_job",
            proxy_id,
            job_timeout=7200,
        )
        _record_enqueued(job, "proxy_generation_job", proxy_id)
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
        _record_enqueued(job, "clip_render_job", clip_id)
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
        _record_enqueued(job, "drive_import_job", import_id)
        return job.id if job else None
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
        job = q.enqueue(
            "app.jobs.watch_folder_sync.watch_folder_sync_job",
            config_id,
            job_timeout=1800,
        )
        _record_enqueued(job, "watch_folder_sync_job", config_id)
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
        _record_enqueued(job, "ugc_product_import_job", product_id)
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
        _record_enqueued(job, "ugc_brief_generate_job", product_id)
        return job.id if job else None
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to enqueue ugc brief gen for product %s: %s", product_id, e)
        return None


def enqueue_ugc_render_job(
    variation_id: int,
    feature_key: str = "ugc_render",
    operation_id: str | None = None,
) -> str | None:
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
        job = q.enqueue(
            "app.jobs.ugc_render.ugc_render_job",
            variation_id,
            feature_key,
            operation_id,
            job_timeout=timeout_sec,
        )
        _record_enqueued(job, "ugc_render_job", variation_id, feature_key=feature_key)
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
        _record_enqueued(job, "ugc_variation_generate_job", campaign_id)
        return job.id if job else None
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to enqueue ugc variation gen for campaign %s: %s", campaign_id, e)
        return None


def enqueue_ai_review_job(video_id: int, options: dict | None = None) -> str | None:
    """Enqueue the multimodal AI review. Returns RQ job id, or None when Redis
    is unset — the caller then runs the review inline so dev without a worker
    still works."""
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        logger.warning("REDIS_URL not set; AI review not enqueued for video %s", video_id)
        return None
    try:
        from redis import Redis
        from rq import Queue

        timeout_sec = max(300, int(os.environ.get("AI_REVIEW_TIMEOUT_SEC", "1800") or "1800"))
        conn = Redis.from_url(url)
        q = Queue("default", connection=conn, default_timeout=timeout_sec)
        job = q.enqueue(
            "app.jobs.ai_review.ai_review_job",
            video_id,
            options,
            job_timeout=timeout_sec,
        )
        _record_enqueued(job, "ai_review_job", video_id)
        return job.id if job else None
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to enqueue AI review for video %s: %s", video_id, e)
        return None


def enqueue_director_job(plan_id: int) -> str | None:
    """Enqueue an AI creative director run. Returns RQ job id, or None when
    Redis is unset — the caller marks the run failed with that reason rather
    than leaving a row queued forever with nothing to pick it up."""
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        logger.warning("REDIS_URL not set; director run %s not enqueued", plan_id)
        return None
    try:
        from redis import Redis
        from rq import Queue

        # Planning is minutes at high effort; asset generation (M3) is longer
        # still, so this is sized for the whole run rather than the first stage.
        timeout_sec = max(900, int(os.environ.get("DIRECTOR_TIMEOUT_SEC", "3600") or "3600"))
        conn = Redis.from_url(url)
        q = Queue("default", connection=conn, default_timeout=timeout_sec)
        job = q.enqueue(
            "app.jobs.director.director_job",
            plan_id,
            job_timeout=timeout_sec,
        )
        _record_enqueued(job, "director_job", plan_id)
        return job.id if job else None
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to enqueue director run %s: %s", plan_id, e)
        return None
