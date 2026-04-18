"""Camera-to-cloud ingest API routes.

Mobile app (or any HTTP client) uploads video files with metadata.
The backend creates a video record, triggers transcription and proxy generation.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import Project, User, Video, VideoProxy
from app.services.project_access import can_access_project
from app.services.storage_policy import assert_storage_upload_allowed
from app.services.ingest_service import ingest_upload
from app.utils.security import get_current_user
from app.api.models.editor_integrations import IngestStatusResponse

router = APIRouter(
    prefix="/ingest",
    tags=["Camera-to-Cloud Ingest"],
)

logger = logging.getLogger(__name__)


@router.post("/upload", response_model=IngestStatusResponse)
def handle_ingest_upload(
    project_id: int = Form(...),
    name: str = Form(...),
    description: Optional[str] = Form(None),
    folder_id: Optional[int] = Form(None),
    device_name: Optional[str] = Form(None),
    location: Optional[str] = Form(None),
    auto_proxy: bool = Form(True),
    video_file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Upload a video from a mobile device or camera-to-cloud app.

    Automatically creates a new video version, triggers transcription,
    and optionally generates a 540p review proxy.
    """
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_access_project(db, current_user.id, project):
        raise HTTPException(status_code=403, detail="Not authorized to upload to this project")

    # Check storage limits
    stream = video_file.file
    if hasattr(stream, "seek") and hasattr(stream, "tell"):
        current = stream.tell()
        stream.seek(0, 2)
        incoming_size = int(stream.tell() or 0)
        stream.seek(current)
    else:
        incoming_size = 0

    try:
        assert_storage_upload_allowed(
            db,
            user=current_user,
            workspace_id=project.workspace_id,
            incoming_bytes=incoming_size,
        )
    except ValueError:
        raise HTTPException(
            status_code=402,
            detail="Storage cap reached. Upgrade plan or add storage to continue uploads.",
        )

    video = ingest_upload(
        db,
        user_id=current_user.id,
        project_id=project_id,
        video_file=video_file,
        name=name,
        description=description,
        folder_id=folder_id,
        device_name=device_name,
        location=location,
        auto_proxy=auto_proxy,
    )

    # Check proxy status
    proxy = (
        db.query(VideoProxy)
        .filter(VideoProxy.video_id == video.id)
        .first()
    )

    return {
        "video_id": video.id,
        "video_name": video.name,
        "version": video.version,
        "file_url": video.file_path,
        "proxy_status": proxy.status if proxy else None,
        "proxy_url": proxy.file_url if proxy and proxy.status == "completed" else None,
        "created_at": video.created_at,
    }


@router.get("/status/{video_id}", response_model=IngestStatusResponse)
def get_ingest_status(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Check the status of an ingested video (proxy generation progress)."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    project = db.query(Project).filter(Project.id == video.project_id).first()
    if not project or not can_access_project(db, current_user.id, project):
        raise HTTPException(status_code=403, detail="Not authorized")

    proxy = (
        db.query(VideoProxy)
        .filter(VideoProxy.video_id == video.id)
        .first()
    )

    return {
        "video_id": video.id,
        "video_name": video.name,
        "version": video.version,
        "file_url": video.file_path,
        "proxy_status": proxy.status if proxy else None,
        "proxy_url": proxy.file_url if proxy and proxy.status == "completed" else None,
        "created_at": video.created_at,
    }
