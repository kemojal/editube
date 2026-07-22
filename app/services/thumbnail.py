"""Server-side video thumbnail generation.

Cloudinary used to derive poster frames on the fly from the video URL
(``so_1,w_640,…``). R2/local URLs can't do that, so we extract a frame with
ffmpeg at upload time and store it as a real object. See migration plan §4.2.
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

from app.storage import build_key, get_storage

logger = logging.getLogger(__name__)


def _ffmpeg_user_agent() -> str:
    # r2.dev bot-protection 403s non-browser User-Agents, so ffmpeg fetching a
    # public R2 URL must send a browser UA. See editube/R2_STORAGE_SETUP.md §6.
    return os.getenv("FFMPEG_USER_AGENT", "Mozilla/5.0")


def generate_thumbnail_to_path(src: str, dst: Path, *, seek: float = 1.0) -> bool:
    """Extract one frame (scaled to 640px wide) from a local path or http(s) URL
    into ``dst`` as JPEG. Returns True on success. ``-ss`` before ``-i`` seeks
    without downloading the whole file for remote inputs."""
    net: list[str] = []
    if src.startswith("http://") or src.startswith("https://"):
        net = ["-user_agent", _ffmpeg_user_agent(), "-rw_timeout", "120000000"]
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{max(0.0, seek):.2f}",
        *net,
        "-i", src,
        "-vframes", "1",
        "-vf", "scale=640:-1",
        "-q:v", "3",
        str(dst),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("thumbnail ffmpeg errored for %s: %s", src, e)
        return False
    if proc.returncode != 0 or not dst.is_file() or dst.stat().st_size == 0:
        logger.warning(
            "thumbnail ffmpeg failed for %s: %s", src, (proc.stderr or proc.stdout or "")[:300]
        )
        return False
    return True


def generate_and_store_thumbnail(
    src: str,
    *,
    folder: str,
    public_id: str,
    seek: float = 1.0,
) -> str | None:
    """Extract a frame from ``src`` and upload it via the active storage backend.
    Returns the public URL, or None on any failure (non-fatal by design)."""
    if not src:
        return None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp) / "thumb.jpg"
            if not generate_thumbnail_to_path(src, dst, seek=seek):
                return None
            key = build_key(folder=folder, public_id=public_id, content_type="image/jpeg")
            return get_storage().upload_path(dst, key=key, content_type="image/jpeg").url
    except Exception:  # noqa: BLE001 — thumbnails are best-effort
        logger.exception("thumbnail generation failed for %s", src)
        return None
