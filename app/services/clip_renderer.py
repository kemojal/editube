"""
Clip render pipeline. Takes a Clip row, cuts the source video to its range,
crops/scales to the target aspect ratio, optionally burns captions via an
ASS file built from the VideoTranscription, and writes an MP4 under
uploads/clips/<clip_id>/output.mp4.

Called from the RQ job app.jobs.clip_render.clip_render_job.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from app.db.models import Clip, ClipStyle, Video, VideoTranscription
from app.services.clip_captions import blocks_from_cuts, to_ass
from app.services.clip_cuts import (
    cuts_bounds,
    cuts_total_duration,
    normalize_cuts,
)
from app.services.youtube_stream_resolve import resolve_youtube_page_to_stream_url

logger = logging.getLogger(__name__)

CLIP_OUTPUT_DIR = Path(os.environ.get("CLIP_OUTPUT_DIR", "./uploads/clips")).resolve()
CLIP_PUBLIC_PREFIX = os.environ.get("CLIP_PUBLIC_PREFIX", "/uploads/clips")

ASPECT_RATIOS = {
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "16:9": (1920, 1080),
}

ProgressCb = Callable[[int], None]


def _run(cmd: list[str]) -> None:
    logger.debug("ffmpeg: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-1500:]
        raise RuntimeError(f"ffmpeg failed ({proc.returncode}): {tail}")


def _download_if_remote(src: str, dst: Path) -> Path:
    # Local-path behavior only. Remote URLs are handled directly by ffmpeg in _cut.
    if src.startswith("http://") or src.startswith("https://"):
        raise ValueError("remote URL should be passed directly to ffmpeg, not downloaded")
    p = Path(src)
    if not p.exists():
        raise FileNotFoundError(f"source video missing: {src}")
    return p


def _cut(src: str | Path, start: float, duration: float, dst: Path) -> None:
    # Re-encode on cut — copy-codec cuts land on keyframes and drift the boundaries.
    src_s = str(src)
    network_args: list[str] = []
    if src_s.startswith("http://") or src_s.startswith("https://"):
        # Microseconds; avoids workers hanging forever on dead/slow streams.
        network_args = ["-rw_timeout", "120000000", "-timeout", "120000000"]
    _run(
        [
            "ffmpeg", "-y",
            "-ss", f"{start:.3f}",
            *network_args,
            "-i", src_s,
            "-t", f"{duration:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(dst),
        ]
    )


def _concat(parts: list[Path], dst: Path, work_dir: Path) -> None:
    """Concatenate pre-encoded parts into `dst` via ffmpeg concat demuxer."""
    if not parts:
        raise RuntimeError("no parts to concat")
    if len(parts) == 1:
        shutil.move(str(parts[0]), dst)
        return
    list_file = work_dir / "concat.txt"
    # Concat demuxer requires POSIX-style paths with single quotes escaped.
    lines = [f"file '{str(p).replace(chr(39), chr(92) + chr(39))}'" for p in parts]
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _run(
        [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(dst),
        ]
    )


def _scale_crop(src: Path, aspect_ratio: str, dst: Path) -> None:
    w, h = ASPECT_RATIOS.get(aspect_ratio, ASPECT_RATIOS["9:16"])
    vf = (
        f"scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h}"
    )
    _run(
        [
            "ffmpeg", "-y",
            "-i", str(src),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(dst),
        ]
    )


def _burn_subs(src: Path, ass_path: Path, dst: Path) -> None:
    # ASS path needs ffmpeg-safe escaping (colon + backslash).
    escaped = str(ass_path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    _run(
        [
            "ffmpeg", "-y",
            "-i", str(src),
            "-vf", f"ass='{escaped}'",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(dst),
        ]
    )


def _extract_thumbnail(src: Path, dst: Path, t: float = 0.5) -> None:
    _run(
        [
            "ffmpeg", "-y",
            "-ss", f"{t:.2f}",
            "-i", str(src),
            "-vframes", "1",
            "-q:v", "3",
            str(dst),
        ]
    )


def _extract_thumbnail_remote(src_url: str, dst: Path, t: float) -> None:
    """Extract a single frame from a local or remote source by URL.

    Uses `-ss` before `-i` so ffmpeg seeks without downloading the whole file
    for http(s) inputs. Callers wrap this in a short timeout.
    """
    network_args: list[str] = []
    if src_url.startswith("http://") or src_url.startswith("https://"):
        network_args = ["-rw_timeout", "120000000", "-timeout", "120000000"]
    _run(
        [
            "ffmpeg", "-y",
            "-ss", f"{max(0.0, t):.2f}",
            *network_args,
            "-i", src_url,
            "-vframes", "1",
            "-q:v", "3",
            str(dst),
        ]
    )


def _is_googlevideo_url(url: str | None) -> bool:
    u = (url or "").lower()
    return "googlevideo.com/videoplayback" in u


def _resolve_video_source_url(db: Session, video: Video) -> str:
    """Return best source URL/path for rendering.

    For YouTube ingests we may store an expiring `googlevideo` stream URL in
    `video.file_path`. Resolve a fresh stream from `video.ingest_page_url` when
    possible so clip renders don't hang on expired URLs.
    """
    raw = str(video.file_path or "").strip()
    page_url = str(video.ingest_page_url or "").strip()
    if page_url and (_is_googlevideo_url(raw) or "youtube.com/" in raw or "youtu.be/" in raw):
        try:
            fresh = resolve_youtube_page_to_stream_url(page_url)
            if fresh:
                video.file_path = fresh
                db.commit()
                return fresh
        except Exception:  # noqa: BLE001
            logger.exception("failed to refresh youtube stream url for video %s", video.id)
            db.rollback()
    return raw


def fast_thumbnail_for_clip(db: "Session", clip_id: int) -> str | None:
    """Best-effort per-clip thumbnail from the source video, so the gallery has
    a real image before the full render completes.

    Returns the public URL of the thumbnail on success, or None.
    """
    try:
        clip: Clip | None = db.query(Clip).filter(Clip.id == clip_id).first()
        if not clip:
            return None
        video: Video | None = db.query(Video).filter(Video.id == clip.video_id).first()
        if not video or not video.file_path:
            return None

        CLIP_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        out_dir = CLIP_OUTPUT_DIR / str(clip.id)
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = out_dir / "thumbnail.jpg"

        start = float(clip.start_time or 0.0)
        end = float(clip.end_time or (start + 1.0))
        t = start + min(1.0, max(0.0, (end - start) / 2.0))

        _extract_thumbnail_remote(video.file_path, dst, t)
        public = f"{CLIP_PUBLIC_PREFIX.rstrip('/')}/{clip.id}/thumbnail.jpg"
        clip.thumbnail_url = public
        db.commit()
        return public
    except Exception:  # noqa: BLE001
        logger.exception("fast thumbnail failed for clip %s", clip_id)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


def _style_dict(style: ClipStyle | None) -> dict:
    if style is None:
        return {}
    return {
        "caption_font": style.caption_font,
        "caption_size": style.caption_size,
        "caption_color": style.caption_color,
        "caption_position": style.caption_position,
        "caption_stroke_color": style.caption_stroke_color,
        "caption_stroke_width": style.caption_stroke_width,
        "caption_font_weight": style.caption_font_weight,
        "caption_position_x": style.caption_position_x,
        "caption_position_y": style.caption_position_y,
        "caption_highlight_color": style.caption_highlight_color,
        "caption_highlight_style": style.caption_highlight_style,
    }


def render_clip(db: Session, clip_id: int, *, on_progress: ProgressCb | None = None) -> str:
    """Full FFmpeg pipeline for one clip. Returns the public path of the output."""
    clip: Clip | None = db.query(Clip).filter(Clip.id == clip_id).first()
    if clip is None:
        raise ValueError(f"clip {clip_id} not found")
    video: Video | None = db.query(Video).filter(Video.id == clip.video_id).first()
    if video is None:
        raise ValueError(f"video {clip.video_id} not found for clip {clip_id}")
    style: ClipStyle | None = (
        db.query(ClipStyle).filter(ClipStyle.clip_id == clip.id).first()
    )

    def report(p: int) -> None:
        if on_progress:
            try:
                on_progress(p)
            except Exception:  # noqa: BLE001
                logger.exception("progress callback failed")

    report(5)
    CLIP_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_dir = CLIP_OUTPUT_DIR / str(clip.id)
    out_dir.mkdir(parents=True, exist_ok=True)

    cuts = normalize_cuts(
        list(clip.cuts or []),
        fallback_start=float(clip.start_time),
        fallback_end=float(clip.end_time),
    )
    if not cuts:
        raise RuntimeError(f"clip {clip.id} has no valid cuts")
    outer_start, outer_end = cuts_bounds(cuts)
    total_duration = cuts_total_duration(cuts)

    with tempfile.TemporaryDirectory(prefix=f"clip-{clip.id}-") as tmp:
        tmp_path = Path(tmp)
        source_url = _resolve_video_source_url(db, video)
        if source_url.startswith("http://") or source_url.startswith("https://"):
            src_input: str | Path = source_url
        else:
            src_input = _download_if_remote(source_url, tmp_path / "source.mp4")
        report(15)

        parts: list[Path] = []
        for idx, cut in enumerate(cuts):
            part = tmp_path / f"part_{idx:02d}.mp4"
            _cut(src_input, float(cut["start"]), float(cut["end"]) - float(cut["start"]), part)
            parts.append(part)
        report(35)

        joined_path = tmp_path / "step1_joined.mp4"
        _concat(parts, joined_path, tmp_path)
        report(50)

        scaled_path = tmp_path / "step2_scaled.mp4"
        _scale_crop(joined_path, clip.aspect_ratio or "9:16", scaled_path)
        report(70)

        final_path = scaled_path
        if style and style.caption_enabled:
            transcription = (
                db.query(VideoTranscription)
                .filter(VideoTranscription.video_id == video.id)
                .first()
            )
            segments = list(transcription.segments or []) if transcription else []
            blocks = blocks_from_cuts(
                segments,
                cuts=cuts,
                words_per_line=style.caption_words_per_line,
                max_lines=style.caption_max_lines,
                uppercase=bool(style.caption_uppercase),
            )
            if blocks:
                target_wh = ASPECT_RATIOS.get(clip.aspect_ratio or "9:16", (1080, 1920))
                ass_text = to_ass(
                    blocks,
                    _style_dict(style),
                    play_res_x=target_wh[0],
                    play_res_y=target_wh[1],
                )
                ass_path = tmp_path / "captions.ass"
                ass_path.write_text(ass_text, encoding="utf-8")
                subbed_path = tmp_path / "step3_subs.mp4"
                _burn_subs(scaled_path, ass_path, subbed_path)
                final_path = subbed_path
        report(85)

        output_file = out_dir / "output.mp4"
        shutil.move(str(final_path), output_file)

        thumb_file = out_dir / "thumbnail.jpg"
        try:
            _extract_thumbnail(output_file, thumb_file, t=min(1.0, total_duration / 2.0))
        except Exception:  # noqa: BLE001
            logger.exception("clip %s thumbnail failed", clip.id)

        report(95)

    public_path = f"{CLIP_PUBLIC_PREFIX.rstrip('/')}/{clip.id}/output.mp4"
    thumb_public = f"{CLIP_PUBLIC_PREFIX.rstrip('/')}/{clip.id}/thumbnail.jpg"
    clip.storage_path = public_path
    clip.thumbnail_url = thumb_public
    clip.start_time = outer_start
    clip.end_time = outer_end
    clip.duration_seconds = total_duration
    db.commit()
    report(100)
    return public_path
