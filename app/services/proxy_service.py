"""Proxy generation and management service.

Provides high-level API for creating, querying, and auto-triggering video proxies.
The actual FFmpeg transcoding runs in a background RQ job (see jobs/proxy_generation.py).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from sqlalchemy.orm import Session

from app.db.models import Video, VideoProxy

logger = logging.getLogger(__name__)

VALID_PROFILES = ("540p_h264", "720p_h264", "1080p_h264")
DEFAULT_PROFILE = os.getenv("PROXY_DEFAULT_PROFILE", "540p_h264").strip()


def create_proxy(db: Session, video_id: int, profile: str = DEFAULT_PROFILE) -> VideoProxy:
    """Create a VideoProxy row and enqueue the generation job.

    Returns the pending proxy row.  Raises ValueError if the profile is
    invalid or a proxy for that (video, profile) already exists.
    """
    if profile not in VALID_PROFILES:
        raise ValueError(f"Invalid proxy profile: {profile}")

    existing = (
        db.query(VideoProxy)
        .filter(VideoProxy.video_id == video_id, VideoProxy.profile == profile)
        .first()
    )
    if existing:
        if existing.status in ("pending", "processing"):
            return existing
        if existing.status == "completed":
            raise ValueError(f"Proxy '{profile}' already exists for video {video_id}")
        # Failed — allow retry by resetting state
        existing.status = "pending"
        existing.error_message = None
        db.commit()
        _enqueue(existing.id)
        return existing

    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise ValueError(f"Video {video_id} not found")

    proxy = VideoProxy(video_id=video_id, profile=profile, status="pending")
    db.add(proxy)
    db.commit()
    db.refresh(proxy)

    _enqueue(proxy.id)
    return proxy


def get_proxy(db: Session, video_id: int, profile: str = DEFAULT_PROFILE) -> Optional[VideoProxy]:
    """Return the proxy row for a video+profile, or None."""
    return (
        db.query(VideoProxy)
        .filter(VideoProxy.video_id == video_id, VideoProxy.profile == profile)
        .first()
    )


def get_proxy_url(db: Session, video_id: int, profile: str = DEFAULT_PROFILE) -> Optional[str]:
    """Return the proxy URL if completed, else None (caller falls back to original)."""
    proxy = get_proxy(db, video_id, profile)
    if proxy and proxy.status == "completed":
        return proxy.file_url
    return None


def list_proxies(db: Session, video_id: int) -> list[VideoProxy]:
    return db.query(VideoProxy).filter(VideoProxy.video_id == video_id).all()


def delete_proxy(db: Session, video_id: int, profile: str) -> bool:
    proxy = get_proxy(db, video_id, profile)
    if not proxy:
        return False
    db.delete(proxy)
    db.commit()
    return True


def auto_proxy_on_upload(db: Session, video_id: int) -> Optional[VideoProxy]:
    """Called after a new video upload to auto-generate the default proxy.

    On by default so the editor gets a scrub-friendly rendition instead of
    playing the full-resolution master; set PROXY_AUTO_GENERATE=0 to disable.
    """
    if os.getenv("PROXY_AUTO_GENERATE", "1").strip() in ("0", "false", "False"):
        logger.debug("Auto-proxy disabled; skipping for video %s", video_id)
        return None

    try:
        return create_proxy(db, video_id, DEFAULT_PROFILE)
    except ValueError as e:
        logger.info("Auto-proxy skipped for video %s: %s", video_id, e)
        return None


def _enqueue(proxy_id: int) -> None:
    try:
        from app.jobs.queue import enqueue_proxy_generation_job

        enqueue_proxy_generation_job(proxy_id)
    except Exception as e:
        logger.warning("Failed to enqueue proxy generation for %s: %s", proxy_id, e)
