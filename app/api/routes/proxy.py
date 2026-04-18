"""Proxy management API routes.

Create, query, and delete video proxies (540p/720p/1080p H.264 transcodes).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import Project, Video, User
from app.services.project_access import can_access_project
from app.services.proxy_service import (
    create_proxy,
    delete_proxy,
    get_proxy,
    list_proxies,
)
from app.utils.security import get_current_user
from app.api.models.editor_integrations import (
    ProxyRequest,
    ProxyResponse,
    ProxyListResponse,
)

router = APIRouter(
    prefix="/proxy",
    tags=["Proxy"],
)

logger = logging.getLogger(__name__)


def _assert_video_access(db: Session, video_id: int, user: User) -> Video:
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    project = db.query(Project).filter(Project.id == video.project_id).first()
    if not project or not can_access_project(db, user.id, project):
        raise HTTPException(status_code=403, detail="Not authorized")
    return video


@router.post("/videos/{video_id}/proxy", response_model=ProxyResponse)
def generate_proxy(
    video_id: int,
    data: ProxyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Trigger proxy generation for a video."""
    _assert_video_access(db, video_id, current_user)

    try:
        proxy = create_proxy(db, video_id, data.profile)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return proxy


@router.get("/videos/{video_id}/proxy", response_model=ProxyListResponse)
def get_video_proxies(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get all proxy renditions for a video."""
    _assert_video_access(db, video_id, current_user)

    proxies = list_proxies(db, video_id)
    return {"video_id": video_id, "proxies": proxies}


@router.get("/videos/{video_id}/proxy/{profile}", response_model=ProxyResponse)
def get_video_proxy(
    video_id: int,
    profile: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get a specific proxy profile for a video."""
    _assert_video_access(db, video_id, current_user)

    proxy = get_proxy(db, video_id, profile)
    if not proxy:
        raise HTTPException(status_code=404, detail=f"No proxy with profile '{profile}' found")
    return proxy


@router.delete("/videos/{video_id}/proxy/{profile}")
def remove_proxy(
    video_id: int,
    profile: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a proxy rendition."""
    _assert_video_access(db, video_id, current_user)

    deleted = delete_proxy(db, video_id, profile)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"No proxy with profile '{profile}' found")
    return {"detail": f"Proxy '{profile}' deleted"}
