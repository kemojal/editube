"""Resolve a YouTube watch/embed URL to a direct media URL (yt-dlp). Used by API and workers."""

from __future__ import annotations

import logging
import json
import subprocess

logger = logging.getLogger(__name__)


class YoutubeStreamResolveError(Exception):
    """yt-dlp could not produce a stream URL."""


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
