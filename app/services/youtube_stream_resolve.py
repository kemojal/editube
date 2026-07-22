"""Resolve a YouTube watch/embed URL to a direct media URL (yt-dlp). Used by API and workers."""

from __future__ import annotations

import logging
import json
import re
import subprocess
import time
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

# Re-resolving a fresh googlevideo stream URL is a real yt-dlp round-trip; skip it
# when the current URL is still comfortably valid. Shared by the D4 stream-refresh
# endpoint (`app/api/routes/video_detail.py`) and the guest review-link proxy
# (`app/services/review_media.py`).
STREAM_REFRESH_MIN_REMAINING_SEC = 600


class YoutubeStreamResolveError(Exception):
    """yt-dlp could not produce a stream URL."""


def stream_url_expire_at(file_path: str | None) -> int | None:
    """Best-effort parse of the `expire` (unix seconds) query param off a
    googlevideo stream URL. Returns None when absent/malformed/non-numeric so
    callers treat it as "unknown -> re-resolve to be safe"."""
    if not file_path:
        return None
    try:
        query = urlparse(file_path).query
        raw = parse_qs(query).get("expire", [None])[0]
        return int(raw) if raw is not None else None
    except (ValueError, TypeError):
        return None


def is_stream_url_stale(
    file_path: str | None, *, min_remaining_sec: int = STREAM_REFRESH_MIN_REMAINING_SEC
) -> bool:
    """True when `file_path`'s `expire` param is absent, malformed, or expiring
    within `min_remaining_sec` — i.e. it should be re-resolved before use."""
    expire_at = stream_url_expire_at(file_path)
    return expire_at is None or expire_at <= time.time() + min_remaining_sec


def _yt_dlp_g_lines(url: str, *format_args: str) -> list[str]:
    raw = (url or "").strip()
    if not raw:
        raise YoutubeStreamResolveError("Empty YouTube URL")
    cmd = ["yt-dlp", "-g", "--no-playlist", *format_args, raw]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except Exception as exc:
        logger.warning("yt-dlp resolve spawn failed: %s", exc)
        raise YoutubeStreamResolveError("Could not run yt-dlp (is it installed?)") from exc

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise YoutubeStreamResolveError(stderr or "yt-dlp failed")

    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if not lines:
        raise YoutubeStreamResolveError("yt-dlp returned no stream URL")
    return lines


def resolve_youtube_page_to_stream_url(url: str) -> str:
    """
    First progressive / combined stream URL (legacy: used when storing `videos.file_path`
    for playback). May be video-only; prefer `resolve_youtube_page_to_audio_stream_url`
    for transcription.
    """
    return _yt_dlp_g_lines(url)[0]


def resolve_youtube_page_to_audio_stream_url(url: str) -> str:
    """
    Direct **audio** stream for Whisper / ffmpeg.
    Tries several yt-dlp format selectors (YouTube varies by video / age-restriction).
    """
    last: YoutubeStreamResolveError | None = None
    for fmt in (
        "bestaudio/best",
        "ba/best",
        "bestaudio[ext=m4a]/bestaudio",
        "bestaudio",
        "140",
        "251",
        "250",
    ):
        try:
            return _yt_dlp_g_lines(url, "-f", fmt)[0]
        except YoutubeStreamResolveError as exc:
            last = exc
            logger.info("yt-dlp audio format %r failed for URL: %s", fmt, exc)
    raise last or YoutubeStreamResolveError("Could not resolve an audio-only stream")


def fetch_youtube_page_metadata(url: str) -> dict:
    raw = (url or "").strip()
    if not raw:
        raise YoutubeStreamResolveError("Empty YouTube URL")
    cmd = ["yt-dlp", "-J", "--no-playlist", raw]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except Exception as exc:
        logger.warning("yt-dlp metadata spawn failed: %s", exc)
        raise YoutubeStreamResolveError("Could not run yt-dlp (is it installed?)") from exc

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise YoutubeStreamResolveError(stderr or "yt-dlp metadata failed")

    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise YoutubeStreamResolveError("yt-dlp returned invalid metadata") from exc
    return data if isinstance(data, dict) else {}


def extract_youtube_video_id(url: str) -> str | None:
    """Best-effort YouTube video ID extraction from watch/shorts/embed/youtu.be URLs."""
    raw = (url or "").strip()
    if not raw:
        return None

    # Users often paste URLs without protocol or with www/mobile host variants.
    if not re.match(r"^https?://", raw, flags=re.IGNORECASE):
        raw = f"https://{raw}"

    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]

    if host == "youtu.be" or host.endswith(".youtu.be"):
        candidate = parsed.path.strip("/").split("/")[0]
        return candidate or None

    if host == "youtube.com" or host.endswith(".youtube.com"):
        if parsed.path == "/watch":
            return parse_qs(parsed.query).get("v", [None])[0]

        if (
            parsed.path.startswith("/shorts/")
            or parsed.path.startswith("/embed/")
            or parsed.path.startswith("/live/")
        ):
            parts = [p for p in parsed.path.split("/") if p]
            return parts[1] if len(parts) > 1 else None

    # Last-resort extraction so pasted snippets like "...watch?v=<id>&..." still work.
    m = re.search(r"(?:v=|/shorts/|/embed/|/live/|youtu\.be/)([A-Za-z0-9_-]{6,})", raw)
    if m:
        return m.group(1)

    return None


def youtube_thumbnail_url(url: str | None) -> str | None:
    video_id = extract_youtube_video_id(url or "")
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else None
