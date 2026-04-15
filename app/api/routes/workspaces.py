"""Workspaces, members, invites, branding, shared library, templates list, capacity."""

from __future__ import annotations

import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import List

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import exists, func, or_
from sqlalchemy.orm import Session, joinedload

from app.api.models.workspaces import (
    CapacityMemberRow,
    ProjectTemplateResponse,
    WorkspaceBrandingUpdate,
    WorkspaceInviteAccept,
    WorkspaceInviteCreate,
    WorkspaceMemberResponse,
    WorkspaceSummaryResponse,
    WorkspaceUpdate,
)
from app.db.database import get_db
from app.db.models import (
    Comment,
    Project,
    ProjectCollaborator,
    ProjectTemplate,
    TimeEntry,
    User,
    Video,
    Workspace,
    WorkspaceAsset,
    WorkspaceBranding,
    WorkspaceInvite,
    WorkspaceMember,
)
from app.services.project_access import get_workspace_member
from app.services.workspace_permissions import (
    can_edit_workspace_branding,
    can_manage_workspace_members,
)
from app.utils.security import get_current_user

router = APIRouter(prefix="/workspaces", tags=["Workspaces"])

ASSET_UPLOAD_SUBDIR = "workspace_assets"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_workspace_member(db: Session, workspace_id: int, user: User) -> WorkspaceMember:
    wm = get_workspace_member(db, workspace_id, user.id)
    if not wm:
        raise HTTPException(status_code=403, detail="Not a member of this workspace")
    return wm


def _workspace_or_404(db: Session, workspace_id: int) -> Workspace:
    ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return ws


@router.get("", response_model=List[WorkspaceSummaryResponse])
def list_my_workspaces(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (
        db.query(WorkspaceMember, Workspace)
        .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
        .filter(WorkspaceMember.user_id == current_user.id)
        .order_by(Workspace.name.asc())
        .all()
    )
    out: list[WorkspaceSummaryResponse] = []
    for wm, ws in rows:
        out.append(
            WorkspaceSummaryResponse(
                id=ws.id,
                name=ws.name,
                slug=ws.slug,
                owner_user_id=ws.owner_user_id,
                role=wm.role,
            )
        )
    return out


@router.get("/{workspace_id}", response_model=WorkspaceSummaryResponse)
def get_workspace(
    workspace_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = _workspace_or_404(db, workspace_id)
    wm = _require_workspace_member(db, workspace_id, current_user)
    return WorkspaceSummaryResponse(
        id=ws.id,
        name=ws.name,
        slug=ws.slug,
        owner_user_id=ws.owner_user_id,
        role=wm.role,
    )


@router.patch("/{workspace_id}", response_model=WorkspaceSummaryResponse)
def update_workspace(
    workspace_id: int,
    body: WorkspaceUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = _workspace_or_404(db, workspace_id)
    wm = _require_workspace_member(db, workspace_id, current_user)
    if wm.role not in ("owner", "producer"):
        raise HTTPException(status_code=403, detail="Not allowed to rename workspace")
    if body.name is not None:
        ws.name = body.name.strip() or ws.name
    db.commit()
    db.refresh(ws)
    return WorkspaceSummaryResponse(
        id=ws.id,
        name=ws.name,
        slug=ws.slug,
        owner_user_id=ws.owner_user_id,
        role=wm.role,
    )


@router.get("/{workspace_id}/members", response_model=List[WorkspaceMemberResponse])
def list_members(
    workspace_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _workspace_or_404(db, workspace_id)
    _require_workspace_member(db, workspace_id, current_user)
    rows = (
        db.query(WorkspaceMember)
        .options(joinedload(WorkspaceMember.user))
        .filter(WorkspaceMember.workspace_id == workspace_id)
        .order_by(WorkspaceMember.created_at.asc())
        .all()
    )
    return [
        WorkspaceMemberResponse(
            user_id=r.user_id,
            role=r.role,
            name=(r.user.name if r.user else None),
            email=(r.user.email if r.user else None),
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.post("/{workspace_id}/invites")
def create_invite(
    workspace_id: int,
    body: WorkspaceInviteCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _workspace_or_404(db, workspace_id)
    wm = _require_workspace_member(db, workspace_id, current_user)
    if not can_manage_workspace_members(wm.role):
        raise HTTPException(status_code=403, detail="Not allowed to invite")
    email = body.email.strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Email required")
    raw = secrets.token_urlsafe(24)
    inv = WorkspaceInvite(
        workspace_id=workspace_id,
        email=email,
        role=(body.role or "editor").strip() or "editor",
        token=raw,
        invited_by_user_id=current_user.id,
        expires_at=_utcnow() + timedelta(days=14),
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return {"token": raw, "expires_at": inv.expires_at.isoformat()}


@router.post("/invites/accept")
def accept_invite(
    body: WorkspaceInviteAccept,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    inv = db.query(WorkspaceInvite).filter(WorkspaceInvite.token == body.token.strip()).first()
    if not inv or inv.accepted_at is not None:
        raise HTTPException(status_code=404, detail="Invite not found")
    if inv.expires_at.replace(tzinfo=timezone.utc) < _utcnow():
        raise HTTPException(status_code=410, detail="Invite expired")
    if (current_user.email or "").lower() != inv.email.lower():
        raise HTTPException(status_code=403, detail="Invite email does not match your account")
    exists = (
        db.query(WorkspaceMember)
        .filter(WorkspaceMember.workspace_id == inv.workspace_id, WorkspaceMember.user_id == current_user.id)
        .first()
    )
    if exists:
        inv.accepted_at = _utcnow()
        db.commit()
        return {"ok": True, "workspace_id": inv.workspace_id}
    db.add(
        WorkspaceMember(
            workspace_id=inv.workspace_id,
            user_id=current_user.id,
            role=inv.role or "editor",
        )
    )
    inv.accepted_at = _utcnow()
    db.commit()
    return {"ok": True, "workspace_id": inv.workspace_id}


@router.delete("/{workspace_id}/members/{user_id}")
def remove_member(
    workspace_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = _workspace_or_404(db, workspace_id)
    wm = _require_workspace_member(db, workspace_id, current_user)
    if not can_manage_workspace_members(wm.role):
        raise HTTPException(status_code=403, detail="Not allowed")
    target = (
        db.query(WorkspaceMember)
        .filter(WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user_id)
        .first()
    )
    if not target:
        raise HTTPException(status_code=404, detail="Member not found")
    if target.role == "owner":
        raise HTTPException(status_code=400, detail="Cannot remove workspace owner")
    db.delete(target)
    db.commit()
    return {"ok": True}


@router.get("/{workspace_id}/project-templates", response_model=List[ProjectTemplateResponse])
def list_project_templates(
    workspace_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _workspace_or_404(db, workspace_id)
    _require_workspace_member(db, workspace_id, current_user)
    rows = (
        db.query(ProjectTemplate)
        .filter(or_(ProjectTemplate.workspace_id.is_(None), ProjectTemplate.workspace_id == workspace_id))
        .order_by(ProjectTemplate.workspace_id.asc().nullsfirst(), ProjectTemplate.name.asc())
        .all()
    )
    return [
        ProjectTemplateResponse(
            id=t.id,
            template_key=t.template_key,
            name=t.name,
            workspace_id=t.workspace_id,
        )
        for t in rows
    ]


@router.get("/{workspace_id}/assets")
def list_assets(
    workspace_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _workspace_or_404(db, workspace_id)
    _require_workspace_member(db, workspace_id, current_user)
    rows = (
        db.query(WorkspaceAsset)
        .filter(WorkspaceAsset.workspace_id == workspace_id)
        .order_by(WorkspaceAsset.created_at.desc())
        .all()
    )
    return [
        {
            "id": a.id,
            "category": a.category,
            "title": a.title,
            "file_url": a.file_url,
            "extra": a.extra,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in rows
    ]


@router.post("/{workspace_id}/assets")
async def upload_asset(
    workspace_id: int,
    title: str = Form(...),
    category: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _workspace_or_404(db, workspace_id)
    wm = _require_workspace_member(db, workspace_id, current_user)
    if wm.role not in ("owner", "producer", "editor"):
        raise HTTPException(status_code=403, detail="Not allowed to upload assets")
    from app.utils.storage import UPLOAD_DIRECTORY

    sub = os.path.join(UPLOAD_DIRECTORY, ASSET_UPLOAD_SUBDIR)
    os.makedirs(sub, exist_ok=True)
    ext = os.path.splitext(file.filename or "")[1] or ""
    safe_name = f"{uuid.uuid4().hex}{ext}"
    dest = os.path.join(sub, safe_name)
    content = await file.read()
    with open(dest, "wb") as fh:
        fh.write(content)
    rel = dest  # store path consistent with other uploads (relative to cwd)
    a = WorkspaceAsset(
        workspace_id=workspace_id,
        category=category.strip() or "other",
        title=title.strip() or "Untitled",
        file_url=rel,
        created_by_user_id=current_user.id,
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    return {"id": a.id, "file_url": a.file_url, "title": a.title, "category": a.category}


@router.delete("/{workspace_id}/assets/{asset_id}")
def delete_asset(
    workspace_id: int,
    asset_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _workspace_or_404(db, workspace_id)
    wm = _require_workspace_member(db, workspace_id, current_user)
    if wm.role not in ("owner", "producer", "editor"):
        raise HTTPException(status_code=403, detail="Not allowed")
    a = (
        db.query(WorkspaceAsset)
        .filter(WorkspaceAsset.id == asset_id, WorkspaceAsset.workspace_id == workspace_id)
        .first()
    )
    if not a:
        raise HTTPException(status_code=404, detail="Asset not found")
    try:
        if a.file_url and os.path.isfile(a.file_url):
            os.remove(a.file_url)
    except OSError:
        pass
    db.delete(a)
    db.commit()
    return {"ok": True}


@router.patch("/{workspace_id}/branding")
def update_branding(
    workspace_id: int,
    body: WorkspaceBrandingUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _workspace_or_404(db, workspace_id)
    wm = _require_workspace_member(db, workspace_id, current_user)
    if not can_edit_workspace_branding(wm.role):
        raise HTTPException(status_code=403, detail="Not allowed to edit branding")
    b = (
        db.query(WorkspaceBranding)
        .filter(WorkspaceBranding.workspace_id == workspace_id)
        .first()
    )
    if not b:
        b = WorkspaceBranding(workspace_id=workspace_id)
        db.add(b)
        db.flush()
    data = body.dict(exclude_unset=True)
    if "custom_domain" in data and data["custom_domain"]:
        data["custom_domain"] = data["custom_domain"].strip().lower()
        b.domain_verified_at = None
    for k, v in data.items():
        setattr(b, k, v)
    db.commit()
    db.refresh(b)
    return {
        "logo_url": b.logo_url,
        "primary_color": b.primary_color,
        "accent_color": b.accent_color,
        "client_footer_text": b.client_footer_text,
        "custom_domain": b.custom_domain,
        "domain_verified_at": b.domain_verified_at.isoformat() if b.domain_verified_at else None,
    }


@router.post("/{workspace_id}/branding/start-domain-verification")
def start_domain_verification(
    workspace_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _workspace_or_404(db, workspace_id)
    wm = _require_workspace_member(db, workspace_id, current_user)
    if not can_edit_workspace_branding(wm.role):
        raise HTTPException(status_code=403, detail="Not allowed")
    b = (
        db.query(WorkspaceBranding)
        .filter(WorkspaceBranding.workspace_id == workspace_id)
        .first()
    )
    if not b:
        b = WorkspaceBranding(workspace_id=workspace_id)
        db.add(b)
        db.flush()
    tok = secrets.token_hex(16)
    b.domain_verification_token = tok
    b.domain_verified_at = None
    db.commit()
    return {
        "verification_token": tok,
        "instructions": "Add a TXT record on your DNS: host _editube-verify value set to the verification_token.",
    }


@router.post("/{workspace_id}/branding/confirm-domain")
def confirm_domain(
    workspace_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Marks domain verified (ops would verify DNS externally); dev-friendly confirm."""

    _workspace_or_404(db, workspace_id)
    wm = _require_workspace_member(db, workspace_id, current_user)
    if not can_edit_workspace_branding(wm.role):
        raise HTTPException(status_code=403, detail="Not allowed")
    b = (
        db.query(WorkspaceBranding)
        .filter(WorkspaceBranding.workspace_id == workspace_id)
        .first()
    )
    if not b or not b.custom_domain:
        raise HTTPException(status_code=400, detail="Set custom_domain first")
    b.domain_verified_at = _utcnow()
    db.commit()
    return {"ok": True, "domain_verified_at": b.domain_verified_at.isoformat()}


@router.get("/{workspace_id}/capacity", response_model=List[CapacityMemberRow])
def workspace_capacity(
    workspace_id: int,
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _workspace_or_404(db, workspace_id)
    _require_workspace_member(db, workspace_id, current_user)
    since = _utcnow() - timedelta(days=days)
    members = (
        db.query(WorkspaceMember)
        .options(joinedload(WorkspaceMember.user))
        .filter(WorkspaceMember.workspace_id == workspace_id)
        .all()
    )
    out: list[CapacityMemberRow] = []
    ws_project_ids = [r[0] for r in db.query(Project.id).filter(Project.workspace_id == workspace_id).all()]
    for m in members:
        if m.role == "client":
            continue
        uid = m.user_id
        collab_exists = exists().where(
            ProjectCollaborator.project_id == Project.id,
            ProjectCollaborator.user_id == uid,
        )
        active_projects = (
            db.query(func.count(Project.id))
            .filter(
                Project.workspace_id == workspace_id,
                or_(Project.creator_id == uid, collab_exists),
            )
            .scalar()
            or 0
        )
        if ws_project_ids:
            seconds = (
                db.query(func.coalesce(func.sum(TimeEntry.duration_seconds), 0))
                .filter(
                    TimeEntry.user_id == uid,
                    TimeEntry.project_id.in_(ws_project_ids),
                    TimeEntry.started_at >= since,
                )
                .scalar()
                or 0
            )
        else:
            seconds = 0
        open_assignments = (
            db.query(func.count(Comment.id))
            .join(Video, Video.id == Comment.video_id)
            .join(Project, Project.id == Video.project_id)
            .filter(
                Project.workspace_id == workspace_id,
                Comment.assignee_user_id == uid,
                Comment.status != "resolved",
                Comment.parent_id.is_(None),
            )
            .scalar()
            or 0
        )
        out.append(
            CapacityMemberRow(
                user_id=uid,
                name=m.user.name if m.user else None,
                email=m.user.email if m.user else None,
                role=m.role,
                active_project_count=int(active_projects),
                tracked_hours=float(seconds) / 3600.0,
                open_assigned_comments=int(open_assignments),
            )
        )
    return out
