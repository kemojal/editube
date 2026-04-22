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

import httpx
from sqlalchemy.orm import Session

from app.db.models import Clip, ClipStyle, Video, VideoTranscription
from app.services.clip_captions import blocks_from_segments, to_ass

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
    if src.startswith("http://") or src.startswith("https://"):
        with httpx.Client(timeout=httpx.Timeout(600.0, connect=30.0)) as client:
            r = client.get(src, follow_redirects=True)
            r.raise_for_status()
            dst.write_bytes(r.content)
        return dst
    p = Path(src)
    if not p.exists():
        raise FileNotFoundError(f"source video missing: {src}")
    return p


def _cut(src: Path, start: float, duration: float, dst: Path) -> None:
    # Re-encode on cut — copy-codec cuts land on keyframes and drift the boundaries.
    _run(
        [
            "ffmpeg", "-y",
            "-ss", f"{start:.3f}",
            "-i", str(src),
            "-t", f"{duration:.3f}",
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

    with tempfile.TemporaryDirectory(prefix=f"clip-{clip.id}-") as tmp:
        tmp_path = Path(tmp)
        src_path = _download_if_remote(video.file_path, tmp_path / "source.mp4")
        report(15)

        start = max(0.0, float(clip.start_time))
        end = max(start + 0.1, float(clip.end_time))
        duration = end - start

        cut_path = tmp_path / "step1_cut.mp4"
        _cut(src_path, start, duration, cut_path)
        report(40)

        scaled_path = tmp_path / "step2_scaled.mp4"
        _scale_crop(cut_path, clip.aspect_ratio or "9:16", scaled_path)
        report(65)

        final_path = scaled_path
        if style and style.caption_enabled:
            transcription = (
                db.query(VideoTranscription)
                .filter(VideoTranscription.video_id == video.id)
                .first()
            )
            segments = list(transcription.segments or []) if transcription else []
            blocks = blocks_from_segments(
                segments,
                clip_start=start,
                clip_end=end,
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
            _extract_thumbnail(output_file, thumb_file, t=min(1.0, duration / 2.0))
        except Exception:  # noqa: BLE001
            logger.exception("clip %s thumbnail failed", clip.id)

        report(95)

    public_path = f"{CLIP_PUBLIC_PREFIX.rstrip('/')}/{clip.id}/output.mp4"
    thumb_public = f"{CLIP_PUBLIC_PREFIX.rstrip('/')}/{clip.id}/thumbnail.jpg"
    clip.storage_path = public_path
    clip.thumbnail_url = thumb_public
    clip.duration_seconds = float(clip.end_time) - float(clip.start_time)
    db.commit()
    report(100)
    return public_path
