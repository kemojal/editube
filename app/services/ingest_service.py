"""Camera-to-cloud and watch-folder ingest service.

Handles receiving uploads from mobile devices and watch folder agents,
creating video records, and triggering proxy generation.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from typing import Optional

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.db.models import Folder, Project, Video, VideoTranscription, WatchFolderConfig

logger = logging.getLogger(__name__)

_FFPROBE = os.getenv("FFPROBE_PATH", "ffprobe").strip()
_MAX_FILE_SIZE_MB = int(os.getenv("WATCH_FOLDER_MAX_FILE_SIZE_MB", "10240"))


def _probe_media(file_path: str) -> dict:
    """Run ffprobe on a file and return parsed metadata.

    Returns dict with keys: duration, width, height, codec, size_bytes.
    Non-fatal — returns empty dict on failure.
    """
    try:
        cmd = [
            _FFPROBE,
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            file_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            logger.warning("ffprobe failed for %s: %s", file_path, result.stderr[:200])
            return {}
        data = json.loads(result.stdout)
        fmt = data.get("format", {})
        video_stream = None
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                video_stream = s
                break
        info: dict = {}
        if fmt.get("duration"):
            info["duration"] = int(float(fmt["duration"]))
        if fmt.get("size"):
            info["size_bytes"] = int(fmt["size"])
        if video_stream:
            info["width"] = int(video_stream.get("width") or 0)
            info["height"] = int(video_stream.get("height") or 0)
            info["codec"] = video_stream.get("codec_name", "")
        return info
    except FileNotFoundError:
        logger.info("ffprobe not found at '%s'; media probing disabled", _FFPROBE)
        return {}
    except Exception as e:
        logger.warning("ffprobe error for %s: %s", file_path, e)
        return {}


def ingest_upload(
    db: Session,
    user_id: int,
    project_id: int,
    video_file: UploadFile,
    *,
    name: str,
    description: Optional[str] = None,
    folder_id: Optional[int] = None,
    device_name: Optional[str] = None,
    location: Optional[str] = None,
    auto_proxy: bool = True,
) -> Video:
    """Ingest a video upload (from mobile app or agent), create video record, and optionally trigger proxy.

    This is the core ingest path — both camera-to-cloud and watch-folder
    uploads funnel through here.
    """
    from app.utils.cloudinary import upload_file_to_cloudinary_with_meta

    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise ValueError(f"Project {project_id} not found")

    if folder_id is not None:
        folder = db.query(Folder).filter(Folder.id == folder_id, Folder.project_id == project_id).first()
        if not folder:
            raise ValueError(f"Folder {folder_id} not found in project {project_id}")

    # Upload to storage (R2 / Cloudinary / local per STORAGE_BACKEND)
    upload_result = upload_file_to_cloudinary_with_meta(video_file)
    file_url = str(upload_result["url"])
    uploaded_size = int(upload_result.get("bytes") or 0)

    # Determine version number
    latest = (
        db.query(Video)
        .filter(Video.project_id == project_id)
        .order_by(Video.version.desc())
        .first()
    )
    version = 1 if not latest else latest.version + 1

    # Try to probe media info from a temp save (best effort)
    duration = None
    try:
        stream = video_file.file
        if hasattr(stream, "seek"):
            stream.seek(0)
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=True) as tmp:
                tmp.write(stream.read())
                tmp.flush()
                info = _probe_media(tmp.name)
                duration = info.get("duration")
            stream.seek(0)
    except Exception:
        pass

    db_video = Video(
        project_id=project_id,
        folder_id=folder_id,
        name=name,
        description=description or f"Uploaded from {device_name or 'mobile'}",
        version=version,
        file_path=file_url,
        size_bytes=uploaded_size,
        duration=duration,
        uploader_id=user_id,
    )
    db.add(db_video)
    db.flush()

    # Create transcription placeholder
    db_tr = VideoTranscription(video_id=db_video.id, status="pending")
    db.add(db_tr)
    db.commit()
    db.refresh(db_video)

    # Trigger transcription
    try:
        from app.jobs.queue import enqueue_transcription_job

        if enqueue_transcription_job(db_video.id):
            row = (
                db.query(VideoTranscription)
                .filter(VideoTranscription.video_id == db_video.id)
                .first()
            )
            if row:
                row.status = "queued"
                db.commit()
    except Exception as e:
        logger.warning("Transcription not enqueued for ingested video %s: %s", db_video.id, e)

    # Poster thumbnail (best-effort; replaces Cloudinary URL-derived thumbnails)
    try:
        from app.jobs.queue import enqueue_video_thumbnail_job

        enqueue_video_thumbnail_job(db_video.id)
    except Exception as e:
        logger.warning("Thumbnail not enqueued for ingested video %s: %s", db_video.id, e)

    # Trigger auto-proxy
    if auto_proxy:
        try:
            from app.services.proxy_service import create_proxy

            create_proxy(db, db_video.id)
        except Exception as e:
            logger.warning("Auto-proxy not triggered for ingested video %s: %s", db_video.id, e)

    return db_video


def check_watch_folder_files(
    db: Session,
    config: WatchFolderConfig,
    files: list[dict],
) -> dict:
    """Compare agent-reported file list against known videos.

    Returns {"new_files": [...], "skipped_files": [...]}.
    """
    # Get existing video names in the project
    existing_names = set(
        name
        for (name,) in db.query(Video.name)
        .filter(Video.project_id == config.project_id)
        .all()
    )

    new_files: list[str] = []
    skipped_files: list[str] = []

    for f in files:
        filename = f.get("filename", "")
        size_mb = (f.get("size_bytes") or 0) / (1024 * 1024)

        if size_mb > _MAX_FILE_SIZE_MB:
            skipped_files.append(filename)
            continue

        # Simple dedup: skip if a video with the same name already exists
        base_name = os.path.splitext(filename)[0]
        if base_name in existing_names or filename in existing_names:
            skipped_files.append(filename)
        else:
            new_files.append(filename)

    return {
        "new_files": new_files,
        "skipped_files": skipped_files,
    }
