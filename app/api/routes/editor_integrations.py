"""NLE integration API routes — session management and marker sync.

Provides the common API surface that all NLE plugins (Premiere, Resolve,
FCP X, After Effects) use for two-way comment ↔ marker synchronisation.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import NLESession, Project, Video, User
from app.services.project_access import can_access_project
from app.utils.security import get_current_user
from app.services.nle_sync_service import (
    export_markers,
    import_markers,
    diff_markers,
    touch_nle_session,
)
from app.api.models.editor_integrations import (
    NLESessionCreate,
    NLESessionResponse,
    MarkerExportResponse,
    MarkerImportRequest,
    MarkerImportResponse,
    MarkerDiffResponse,
    MarkerItem,
)

router = APIRouter(
    prefix="/integrations",
    tags=["Editor Integrations"],
)

logger = logging.getLogger(__name__)


# ── NLE Session Management ────────────────────────────────────────────


@router.post("/nle/sessions", response_model=NLESessionResponse)
def create_nle_session(
    data: NLESessionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Register an NLE session (called when a plugin starts up)."""
    project = db.query(Project).filter(Project.id == data.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_access_project(db, current_user.id, project):
        raise HTTPException(status_code=403, detail="Not authorized")

    session = NLESession(
        user_id=current_user.id,
        project_id=data.project_id,
        nle_type=data.nle_type,
        nle_version=data.nle_version,
        host_name=data.host_name,
        sync_direction=data.sync_direction,
        is_active=True,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


@router.get("/nle/sessions", response_model=list[NLESessionResponse])
def list_nle_sessions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List active NLE sessions for the current user."""
    sessions = (
        db.query(NLESession)
        .filter(NLESession.user_id == current_user.id, NLESession.is_active == True)
        .order_by(NLESession.created_at.desc())
        .all()
    )
    return sessions


@router.delete("/nle/sessions/{session_id}")
def delete_nle_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Deregister an NLE session (plugin shutdown)."""
    session = (
        db.query(NLESession)
        .filter(NLESession.id == session_id, NLESession.user_id == current_user.id)
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    session.is_active = False
    db.commit()
    return {"detail": "Session deregistered"}


# ── Marker Sync ───────────────────────────────────────────────────────


@router.get("/nle/{video_id}/markers", response_model=MarkerExportResponse)
def get_markers(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Export video comments as NLE markers (JSON)."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    project = db.query(Project).filter(Project.id == video.project_id).first()
    if not project or not can_access_project(db, current_user.id, project):
        raise HTTPException(status_code=403, detail="Not authorized")

    markers = export_markers(db, video_id)
    return {
        "video_id": video_id,
        "marker_count": len(markers),
        "markers": markers,
        "exported_at": datetime.now(timezone.utc),
    }


@router.post("/nle/{video_id}/markers", response_model=MarkerImportResponse)
def push_markers(
    video_id: int,
    data: MarkerImportRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Import markers from an NLE as comments."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    project = db.query(Project).filter(Project.id == video.project_id).first()
    if not project or not can_access_project(db, current_user.id, project):
        raise HTTPException(status_code=403, detail="Not authorized")

    marker_dicts = [m.model_dump() for m in data.markers]
    result = import_markers(
        db,
        video_id,
        current_user.id,
        marker_dicts,
        data.source_nle,
        replace_existing=data.replace_existing,
    )
    return {"video_id": video_id, **result}


@router.post("/nle/{video_id}/sync", response_model=MarkerExportResponse)
def sync_markers(
    video_id: int,
    data: MarkerImportRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Full bidirectional sync — import markers from NLE, then return current state."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    project = db.query(Project).filter(Project.id == video.project_id).first()
    if not project or not can_access_project(db, current_user.id, project):
        raise HTTPException(status_code=403, detail="Not authorized")

    # Import incoming
    marker_dicts = [m.model_dump() for m in data.markers]
    import_markers(
        db,
        video_id,
        current_user.id,
        marker_dicts,
        data.source_nle,
        replace_existing=data.replace_existing,
    )

    # Export current state
    markers = export_markers(db, video_id)
    return {
        "video_id": video_id,
        "marker_count": len(markers),
        "markers": markers,
        "exported_at": datetime.now(timezone.utc),
    }


@router.get("/nle/{video_id}/markers/diff", response_model=MarkerDiffResponse)
def get_markers_diff(
    video_id: int,
    since: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get marker changes since a given ISO timestamp."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    project = db.query(Project).filter(Project.id == video.project_id).first()
    if not project or not can_access_project(db, current_user.id, project):
        raise HTTPException(status_code=403, detail="Not authorized")

    try:
        since_dt = datetime.fromisoformat(since)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid 'since' timestamp — use ISO 8601")

    result = diff_markers(db, video_id, since_dt)
    return {
        "video_id": video_id,
        "since": since_dt,
        **result,
    }
