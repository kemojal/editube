"""Watch folder API routes.

Allows desktop agents to register watched folders, report file lists,
and upload newly detected files for auto-versioning.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import Project, User, WatchFolderConfig
from app.services.project_access import can_access_project
from app.services.ingest_service import check_watch_folder_files, ingest_upload
from app.utils.security import get_current_user
from app.api.models.editor_integrations import (
    WatchFolderCreate,
    WatchFolderUpdate,
    WatchFolderResponse,
    WatchFolderSyncRequest,
    WatchFolderSyncResponse,
)

router = APIRouter(
    prefix="/watch-folders",
    tags=["Watch Folders"],
)

logger = logging.getLogger(__name__)


@router.post("/", response_model=WatchFolderResponse)
def create_watch_folder(
    data: WatchFolderCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a watch folder configuration."""
    project = db.query(Project).filter(Project.id == data.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_access_project(db, current_user.id, project):
        raise HTTPException(status_code=403, detail="Not authorized")

    config = WatchFolderConfig(
        user_id=current_user.id,
        project_id=data.project_id,
        folder_path=data.folder_path,
        auto_proxy=data.auto_proxy,
        auto_version=data.auto_version,
        file_pattern=data.file_pattern,
    )
    db.add(config)
    db.commit()
    db.refresh(config)
    return config


@router.get("/", response_model=list[WatchFolderResponse])
def list_watch_folders(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List the current user's watch folder configs."""
    configs = (
        db.query(WatchFolderConfig)
        .filter(WatchFolderConfig.user_id == current_user.id)
        .order_by(WatchFolderConfig.created_at.desc())
        .all()
    )
    return configs


@router.put("/{config_id}", response_model=WatchFolderResponse)
def update_watch_folder(
    config_id: int,
    data: WatchFolderUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update a watch folder configuration."""
    config = (
        db.query(WatchFolderConfig)
        .filter(WatchFolderConfig.id == config_id, WatchFolderConfig.user_id == current_user.id)
        .first()
    )
    if not config:
        raise HTTPException(status_code=404, detail="Watch folder config not found")

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(config, field, value)

    db.commit()
    db.refresh(config)
    return config


@router.delete("/{config_id}")
def delete_watch_folder(
    config_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a watch folder configuration."""
    config = (
        db.query(WatchFolderConfig)
        .filter(WatchFolderConfig.id == config_id, WatchFolderConfig.user_id == current_user.id)
        .first()
    )
    if not config:
        raise HTTPException(status_code=404, detail="Watch folder config not found")

    db.delete(config)
    db.commit()
    return {"detail": "Watch folder deleted"}


@router.post("/{config_id}/sync", response_model=WatchFolderSyncResponse)
def sync_watch_folder(
    config_id: int,
    data: WatchFolderSyncRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Report detected files from the desktop agent; get back list of files to upload."""
    config = (
        db.query(WatchFolderConfig)
        .filter(WatchFolderConfig.id == config_id, WatchFolderConfig.user_id == current_user.id)
        .first()
    )
    if not config:
        raise HTTPException(status_code=404, detail="Watch folder config not found")

    file_dicts = [f.model_dump() for f in data.files]
    result = check_watch_folder_files(db, config, file_dicts)

    config.last_sync_at = datetime.now(timezone.utc)
    db.commit()

    return {
        "config_id": config.id,
        "new_files": result["new_files"],
        "skipped_files": result["skipped_files"],
        "upload_urls": [],  # Direct upload — agent will POST to /{config_id}/upload
    }


@router.post("/{config_id}/upload")
def upload_watch_folder_file(
    config_id: int,
    video_file: UploadFile = File(...),
    name: str = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Upload a file detected by the watch folder agent."""
    config = (
        db.query(WatchFolderConfig)
        .filter(WatchFolderConfig.id == config_id, WatchFolderConfig.user_id == current_user.id)
        .first()
    )
    if not config:
        raise HTTPException(status_code=404, detail="Watch folder config not found")

    file_name = name or video_file.filename or "watch_upload"

    video = ingest_upload(
        db,
        user_id=current_user.id,
        project_id=config.project_id,
        video_file=video_file,
        name=file_name,
        description=f"Auto-uploaded from watch folder: {config.folder_path}",
        auto_proxy=config.auto_proxy,
    )

    return {
        "video_id": video.id,
        "name": video.name,
        "version": video.version,
        "file_url": video.file_path,
        "project_id": config.project_id,
    }
