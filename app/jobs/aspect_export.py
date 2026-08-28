"""
RQ job: center-crop + scale video to target aspect ratio (ffmpeg), upload to Cloudinary.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

import cloudinary.uploader
import httpx
from sqlalchemy.orm import Session

import app.utils.cloudinary  # noqa: F401 — loads Cloudinary config
from app.db.database import SessionLocal
from app.db.models import Video, VideoAspectExport

logger = logging.getLogger(__name__)

# target (width, height) for vertical/square exports at ~1080p class resolution
_ASPECT_TARGETS: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "16:9": (1920, 1080),
    "4:5": (1080, 1350),
}


def _target_size(aspect_ratio: str) -> tuple[int, int]:
    key = (aspect_ratio or "").strip()
    return _ASPECT_TARGETS.get(key, _ASPECT_TARGETS["9:16"])


def _safe_margin_fraction(preset: str | None) -> float:
    """Slightly inset crop for Shorts/Reels safe areas."""
    p = (preset or "").lower()
    if p in ("shorts", "reels", "tiktok"):
        return 0.04
    return 0.0


def aspect_export_job(export_id: int) -> None:
    db: Session = SessionLocal()
    try:
        exp = db.query(VideoAspectExport).filter(VideoAspectExport.id == export_id).first()
        if not exp:
            logger.error("aspect_export_job: export %s not found", export_id)
            raise RuntimeError(f"Aspect export {export_id} not found")

        video = db.query(Video).filter(Video.id == exp.video_id).first()
        if not video or not video.file_path:
            _fail(db, exp, "Video or file_path missing.")
            raise RuntimeError("Video or file_path missing")

        exp.status = "processing"
        exp.error_message = None
        db.add(exp)
        db.commit()

        tw, th = _target_size(exp.aspect_ratio)
        margin = _safe_margin_fraction(exp.platform_preset)
        if margin > 0:
            tw = int(tw * (1 - 2 * margin))
            th = int(th * (1 - 2 * margin))
            tw = max(tw, 2)
            th = max(th, 2)

        vf = f"scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th}"

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "src.bin"
            dst = tmp_path / "out.mp4"

            with httpx.Client(timeout=httpx.Timeout(600.0, connect=60.0)) as client:
                r = client.get(video.file_path, follow_redirects=True)
                r.raise_for_status()
                src.write_bytes(r.content)

            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                str(src),
                "-vf",
                vf,
                "-c:v",
                "libx264",
                "-preset",
                os.environ.get("ASPECT_EXPORT_FFMPEG_PRESET", "fast"),
                "-crf",
                os.environ.get("ASPECT_EXPORT_CRF", "23"),
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                str(dst),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "")[:4000]
                _fail(db, exp, f"ffmpeg failed: {err}")
                raise RuntimeError("Aspect export ffmpeg failed")

            from app.storage import build_key, get_storage, guess_content_type

            _ct = guess_content_type(str(dst), resource_type="video")
            _key = build_key(
                folder=os.environ.get("CLOUDINARY_ASPECT_FOLDER", "aspect_exports"),
                filename=dst.name,
                content_type=_ct,
            )
            url = get_storage().upload_path(dst, key=_key, content_type=_ct).url
            if not url:
                _fail(db, exp, "Storage returned no URL.")
                raise RuntimeError("Aspect export storage returned no URL")

            exp.status = "completed"
            exp.output_path = url
            exp.duration = video.duration
            exp.error_message = None
            db.add(exp)
            db.commit()
            logger.info("Aspect export %s completed -> %s", export_id, url)

    except Exception as e:
        logger.exception("aspect_export_job failed for %s", export_id)
        try:
            exp = db.query(VideoAspectExport).filter(VideoAspectExport.id == export_id).first()
            if exp:
                _fail(db, exp, str(e)[:4000])
        except Exception:
            pass
        raise
    finally:
        db.close()


def _fail(db: Session, exp: VideoAspectExport, message: str) -> None:
    try:
        exp.status = "failed"
        exp.error_message = message[:4000]
        db.add(exp)
        db.commit()
    except Exception:
        logger.exception("Could not persist aspect export failure for %s", exp.id)
