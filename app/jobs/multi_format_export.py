"""RQ job: export fixed delivery renditions (4k/1080p/720p)."""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

import cloudinary.uploader
import httpx
from sqlalchemy.orm import Session

import app.utils.cloudinary  # noqa: F401
from app.db.database import SessionLocal
from app.db.models import DeliveryExport, Video

logger = logging.getLogger(__name__)

_PROFILES: dict[str, dict[str, str | int]] = {
    "4k_master": {"width": 3840, "height": 2160, "video_bitrate": "22000k", "audio_bitrate": "320k"},
    "yt_1080p": {"width": 1920, "height": 1080, "video_bitrate": "8000k", "audio_bitrate": "192k"},
    "social_720p": {"width": 1280, "height": 720, "video_bitrate": "4500k", "audio_bitrate": "128k"},
}


def multi_format_export_job(export_id: int) -> None:
    db: Session = SessionLocal()
    try:
        exp = db.query(DeliveryExport).filter(DeliveryExport.id == export_id).first()
        if not exp:
            logger.error("multi_format_export_job: export %s not found", export_id)
            return
        profile = _PROFILES.get(exp.profile_key)
        if not profile:
            _fail(db, exp, f"Unknown profile key: {exp.profile_key}")
            return

        video = db.query(Video).filter(Video.id == exp.video_id).first()
        if not video or not video.file_path:
            _fail(db, exp, "Video or file_path missing.")
            return

        exp.status = "processing"
        exp.error_message = None
        db.add(exp)
        db.commit()

        width = int(profile["width"])
        height = int(profile["height"])

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "src.bin"
            dst = tmp_path / f"{exp.profile_key}.mp4"

            with httpx.Client(timeout=httpx.Timeout(600.0, connect=60.0)) as client:
                r = client.get(video.file_path, follow_redirects=True)
                r.raise_for_status()
                src.write_bytes(r.content)

            vf = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
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
                os.environ.get("DELIVERY_EXPORT_FFMPEG_PRESET", "medium"),
                "-b:v",
                str(profile["video_bitrate"]),
                "-maxrate",
                str(profile["video_bitrate"]),
                "-bufsize",
                str(profile["video_bitrate"]),
                "-c:a",
                "aac",
                "-b:a",
                str(profile["audio_bitrate"]),
                str(dst),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "")[:4000]
                _fail(db, exp, f"ffmpeg failed: {err}")
                return

            upload = cloudinary.uploader.upload(
                str(dst),
                resource_type="video",
                folder=os.environ.get("CLOUDINARY_DELIVERY_EXPORT_FOLDER", "delivery_exports"),
            )
            url = upload.get("secure_url")
            if not url:
                _fail(db, exp, "Cloudinary returned no URL.")
                return

            exp.status = "completed"
            exp.output_path = url
            exp.mime_type = "video/mp4"
            exp.width = width
            exp.height = height
            exp.size_bytes = dst.stat().st_size if dst.exists() else None
            exp.error_message = None
            db.add(exp)
            db.commit()
    except Exception as e:
        logger.exception("multi_format_export_job failed for %s", export_id)
        try:
            exp = db.query(DeliveryExport).filter(DeliveryExport.id == export_id).first()
            if exp:
                _fail(db, exp, str(e)[:4000])
        except Exception:
            pass
    finally:
        db.close()


def _fail(db: Session, exp: DeliveryExport, message: str) -> None:
    exp.status = "failed"
    exp.error_message = message[:4000]
    db.add(exp)
    db.commit()
