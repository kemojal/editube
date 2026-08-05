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


# The progressive (muxed: one file, video AND audio) YouTube formats. Everything
# else googlevideo serves is DASH — video-only or audio-only — which a plain
# `<video src>` renders as a black frame with a running clock, or as sound with
# no picture. Legacy flv/3gp itags are included for completeness; in practice
# YouTube serves 18 (360p avc1+aac) and sometimes 22 (720p).
MUXED_ITAGS = frozenset(
    {5, 6, 17, 18, 22, 34, 35, 36, 37, 38, 43, 44, 45, 46, 59, 78, 82, 83, 84, 85, 100, 101, 102}
)


def stream_url_itag(file_path: str | None) -> int | None:
    """The `itag` (YouTube format id) of a googlevideo stream URL, else None."""
    if not file_path:
        return None
    try:
        raw = parse_qs(urlparse(file_path).query).get("itag", [None])[0]
        return int(raw) if raw is not None else None
    except (ValueError, TypeError):
        return None


def is_stream_url_muxed(file_path: str | None) -> bool:
    """
    True when `file_path` can carry both picture and sound on its own.

    A URL with no itag isn't a googlevideo stream at all (an upload, a Cloudinary
    asset, a local `/uploads/...` path), so it is taken at face value — this
    heuristic exists only to catch DASH streams standing in for playback.
    """
    itag = stream_url_itag(file_path)
    return itag is None or itag in MUXED_ITAGS


def should_refresh_stream_url(
    file_path: str | None, *, min_remaining_sec: int = STREAM_REFRESH_MIN_REMAINING_SEC
) -> bool:
    """
    True when a stored playback URL has to be re-resolved: either it is expiring
    (or already dead), or it is a DASH stream that cannot play on its own.

    The second case is what makes an already-broken row heal itself instead of
    staying black until its `expire` runs out.
    """
    return is_stream_url_stale(file_path, min_remaining_sec=min_remaining_sec) or not (
        is_stream_url_muxed(file_path)
    )


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


# Playback selectors, most to least desirable. h264+aac first: it is the one
# combination every browser decodes, at every resolution, without hardware
# surprises (a 2160p AV1 stream decodes to a black frame on plenty of machines).
_PLAYBACK_FORMAT_SELECTORS = (
    "best[ext=mp4][vcodec^=avc1][acodec!=none]",
    "best[ext=mp4][vcodec!=none][acodec!=none]",
    "best[vcodec!=none][acodec!=none]",
    "22/18",
    "18",
)


def resolve_youtube_page_to_stream_url(url: str) -> str:
    """
    A **muxed** progressive stream URL — video and audio in one file — for
    storing as `videos.file_path` and handing straight to a `<video>` element.

    This must ask for a format explicitly. yt-dlp's default is
    `bestvideo*+bestaudio`, so a bare `yt-dlp -g` prints two URLs (video-only
    then audio-only) and taking the first stored a silent, often-undecodable
    DASH stream: the viewer went black while the clock ran, and nothing errored
    because the URL itself served 206 video/mp4 perfectly well.

    Falls back through `_PLAYBACK_FORMAT_SELECTORS`, then — rather than leave a
    video with no URL at all — to yt-dlp's default first line, the old
    behaviour. Prefer `resolve_youtube_page_to_audio_stream_url` for
    transcription, which wants audio only.
    """
    last: YoutubeStreamResolveError | None = None
    for fmt in _PLAYBACK_FORMAT_SELECTORS:
        try:
            return _yt_dlp_g_lines(url, "-f", fmt)[0]
        except YoutubeStreamResolveError as exc:
            last = exc
            logger.info("yt-dlp playback format %r unavailable: %s", fmt, exc)

    logger.warning(
        "No muxed stream for %s; falling back to yt-dlp's default format (playback may be silent)",
        url,
    )
    try:
        return _yt_dlp_g_lines(url)[0]
    except YoutubeStreamResolveError:
        raise last or YoutubeStreamResolveError("Could not resolve a playable stream")


def resolve_youtube_page_to_best_video_stream_url(url: str) -> str:
    """
    Highest-quality video stream, **which may be video-only** (no audio track).

    yt-dlp's default `bestvideo*+bestaudio` selection, first line — i.e. what
    every caller used to get from `resolve_youtube_page_to_stream_url`. Kept for
    the ffmpeg pipelines (clip render, mask tracking) that were built against
    it: they want resolution over a muxed container, and switching them to a
    muxed 360p stream would quietly downgrade every export.

    Never store the result as a video's playback URL — a browser renders it as
    a black frame with a running clock. Use `resolve_youtube_page_to_stream_url`
    for that.
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
