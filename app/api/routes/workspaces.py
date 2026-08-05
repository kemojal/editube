"""Workspaces, members, invites, branding, shared library, templates list, capacity."""

from __future__ import annotations

import math
import mimetypes
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import List

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import exists, func, or_
from urllib.parse import quote
from sqlalchemy.orm import Session, joinedload

from app.api.models.workspaces import (
    CapacityMemberRow,
    ProjectTemplateResponse,
    WorkspaceAssetUpdate,
    WorkspaceBrandingResponse,
    WorkspaceBrandingUpdate,
    WorkspaceInviteAccept,
    WorkspaceInviteCreate,
    WorkspaceInviteCreatedResponse,
    WorkspaceInviteListItem,
    MyPendingWorkspaceInviteItem,
    WorkspaceMemberResponse,
    WorkspaceProvisionMemberBody,
    WorkspaceProvisionMemberResponse,
    WorkspaceSummaryResponse,
    WorkspaceUpdate,
    WorkspaceSSOProviderCreate,
    WorkspaceSSOProviderResponse,
    WorkspaceAuthPolicyUpdate,
    WorkspaceAuthPolicyResponse,
    NDADocumentCreate,
    NDADocumentResponse,
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
    WorkspaceSSOProvider,
    WorkspaceAuthPolicy,
    NDADocument,
    Notification,
)
from app.services.project_access import get_workspace_member
from app.services.workspace_roles import normalize_invite_role
from app.services.workspace_permissions import (
    can_edit_workspace_branding,
    can_manage_workspace_members,
)
from app.services.dns_domain_verify import verify_editube_domain_txt
from app.services.workspace_bootstrap import ensure_personal_workspace
from app.utils.email import send_workspace_invite_email, send_workspace_provisioned_account_email
from app.utils.security import get_current_user, get_password_hash
from app.services.oidc_sso import discover_oidc_metadata
from app.services.security_audit import log_security_audit_event
from app.jobs.queue import enqueue_push_notification_job
from app.websocket_manager import notifications_ws_manager

router = APIRouter(prefix="/workspaces", tags=["Workspaces"])

ASSET_UPLOAD_SUBDIR = "workspace_assets"
ALLOWED_ASSET_CATEGORIES = frozenset(
    {"logo", "lut", "music", "sfx", "lower_third", "other"}
)


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


def _public_app_base() -> str:
    return (
        os.getenv("WORKSPACE_INVITE_PUBLIC_BASE_URL")
        or os.getenv("FRONTEND_BASE_URL")
        or "https://knee-quote-doing-sword.trycloudflare.com"
    ).rstrip("/")


def _workspace_invite_accept_url(token: str) -> str:
    base = _public_app_base()
    return f"{base}/account/team?tab=members&invite={quote(token, safe='')}"


def _invite_status(inv: WorkspaceInvite, now: datetime) -> str:
    if inv.accepted_at is not None:
        return "accepted"
    exp = inv.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp < now:
        return "expired"
    return "pending"


@router.get("", response_model=List[WorkspaceSummaryResponse])
def list_my_workspaces(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (
        db.query(WorkspaceMember, Workspace, User)
        .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
        .outerjoin(User, User.id == Workspace.owner_user_id)
        .filter(WorkspaceMember.user_id == current_user.id)
        .order_by(Workspace.name.asc())
        .all()
    )
    out: list[WorkspaceSummaryResponse] = []
    for wm, ws, owner in rows:
        owner_name = None
        if owner:
            owner_name = (owner.full_name or owner.name or "").strip() or None
        out.append(
            WorkspaceSummaryResponse(
                id=ws.id,
                name=ws.name,
                slug=ws.slug,
                owner_user_id=ws.owner_user_id,
                owner_name=owner_name,
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
    owner = db.query(User).filter(User.id == ws.owner_user_id).first()
    owner_name = ((owner.full_name or owner.name or "").strip() or None) if owner else None
    return WorkspaceSummaryResponse(
        id=ws.id,
        name=ws.name,
        slug=ws.slug,
        owner_user_id=ws.owner_user_id,
        owner_name=owner_name,
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
    log_security_audit_event(
        db,
        action="workspace.update",
        resource_type="workspace",
        resource_id=str(workspace_id),
        actor_user_id=current_user.id,
        actor_type="user",
        workspace_id=workspace_id,
        metadata={"updated_name": bool(body.name)},
    )
    db.commit()
    db.refresh(ws)
    owner = db.query(User).filter(User.id == ws.owner_user_id).first()
    owner_name = ((owner.full_name or owner.name or "").strip() or None) if owner else None
    return WorkspaceSummaryResponse(
        id=ws.id,
        name=ws.name,
        slug=ws.slug,
        owner_user_id=ws.owner_user_id,
        owner_name=owner_name,
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
            avatar_url=(r.user.avatar_url if r.user else None),
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.get("/{workspace_id}/invites", response_model=List[WorkspaceInviteListItem])
def list_workspace_invites(
    workspace_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _workspace_or_404(db, workspace_id)
    _require_workspace_member(db, workspace_id, current_user)
    rows = (
        db.query(WorkspaceInvite)
        .filter(WorkspaceInvite.workspace_id == workspace_id)
        .order_by(WorkspaceInvite.created_at.desc())
        .limit(200)
        .all()
    )
    now = _utcnow()
    return [
        WorkspaceInviteListItem(
            id=r.id,
            email=r.email,
            role=r.role,
            expires_at=r.expires_at,
            accepted_at=r.accepted_at,
            created_at=r.created_at,
            status=_invite_status(r, now),
        )
        for r in rows
    ]


@router.get("/invites/me/pending", response_model=List[MyPendingWorkspaceInviteItem])
def list_my_pending_invites(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    email = (current_user.email or "").strip().lower()
    if not email:
        return []
    now = _utcnow()
    already_member = exists().where(
        WorkspaceMember.workspace_id == WorkspaceInvite.workspace_id,
        WorkspaceMember.user_id == current_user.id,
    )
    rows = (
        db.query(WorkspaceInvite, Workspace, User)
        .join(Workspace, Workspace.id == WorkspaceInvite.workspace_id)
        .outerjoin(User, User.id == WorkspaceInvite.invited_by_user_id)
        .filter(
            WorkspaceInvite.accepted_at.is_(None),
            WorkspaceInvite.email == email,
            WorkspaceInvite.expires_at >= now,
            ~already_member,
        )
        .order_by(WorkspaceInvite.created_at.desc())
        .all()
    )
    return [
        MyPendingWorkspaceInviteItem(
            id=invite.id,
            workspace_id=invite.workspace_id,
            workspace_name=workspace.name,
            email=invite.email,
            role=invite.role,
            token=invite.token,
            expires_at=invite.expires_at,
            invited_by_name=((inviter.name or inviter.email) if inviter else None),
            created_at=invite.created_at,
            status=_invite_status(invite, now),
        )
        for invite, workspace, inviter in rows
    ]


async def _emit_workspace_invite_notification(
    db: Session,
    *,
    invite: WorkspaceInvite,
    workspace: Workspace,
    inviter: User,
    invited_user: User | None,
) -> None:
    if invited_user is None:
        return
    msg = f"{inviter.name or inviter.email or 'A teammate'} invited you to join {workspace.name}"
    existing_unread = (
        db.query(Notification)
        .filter(
            Notification.user_id == invited_user.id,
            Notification.type == "workspace_invite",
            Notification.workspace_invite_id == invite.id,
            Notification.read.is_(False),
        )
        .first()
    )
    if existing_unread:
        existing_unread.message = msg
        existing_unread.invite_token = invite.token
        existing_unread.workspace_id = workspace.id
        db.commit()
        db.refresh(existing_unread)
        enqueue_push_notification_job(existing_unread.user_id, existing_unread.id)
        await notifications_ws_manager.send_to_user(
            existing_unread.user_id,
            {
                "event": "notification.new",
                "payload": {
                    "id": existing_unread.id,
                    "type": existing_unread.type,
                    "read": existing_unread.read,
                    "project_id": existing_unread.project_id,
                    "video_id": existing_unread.video_id,
                    "comment_id": existing_unread.comment_id,
                    "workspace_id": existing_unread.workspace_id,
                    "workspace_invite_id": existing_unread.workspace_invite_id,
                    "invite_token": existing_unread.invite_token,
                    "message": existing_unread.message,
                    "created_at": existing_unread.created_at.isoformat() if existing_unread.created_at else None,
                },
            },
        )
        return
    notification = Notification(
        user_id=invited_user.id,
        type="workspace_invite",
        workspace_id=workspace.id,
        workspace_invite_id=invite.id,
        invite_token=invite.token,
        message=msg,
        read=False,
    )
    db.add(notification)
    db.commit()
    db.refresh(notification)
    enqueue_push_notification_job(notification.user_id, notification.id)
    await notifications_ws_manager.send_to_user(
        notification.user_id,
        {
            "event": "notification.new",
            "payload": {
                "id": notification.id,
                "type": notification.type,
                "read": notification.read,
                "project_id": notification.project_id,
                "video_id": notification.video_id,
                "comment_id": notification.comment_id,
                "workspace_id": notification.workspace_id,
                "workspace_invite_id": notification.workspace_invite_id,
                "invite_token": notification.invite_token,
                "message": notification.message,
                "created_at": notification.created_at.isoformat() if notification.created_at else None,
            },
        },
    )


@router.post("/{workspace_id}/invites", response_model=WorkspaceInviteCreatedResponse)
async def create_invite(
    workspace_id: int,
    body: WorkspaceInviteCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = _workspace_or_404(db, workspace_id)
    wm = _require_workspace_member(db, workspace_id, current_user)
    if not can_manage_workspace_members(wm.role):
        raise HTTPException(status_code=403, detail="Not allowed to invite")
    email = body.email.strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Email required")
    if "@" not in email or len(email) < 4:
        raise HTTPException(status_code=400, detail="Invalid email address")

    existing_user = db.query(User).filter(func.lower(User.email) == email).first()
    if existing_user:
        already = (
            db.query(WorkspaceMember)
            .filter(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == existing_user.id,
            )
            .first()
        )
        if already:
            raise HTTPException(
                status_code=400,
                detail="That user is already a member of this workspace",
            )

    db.query(WorkspaceInvite).filter(
        WorkspaceInvite.workspace_id == workspace_id,
        WorkspaceInvite.email == email,
        WorkspaceInvite.accepted_at.is_(None),
    ).delete(synchronize_session=False)

    raw = secrets.token_urlsafe(24)
    invite_role = normalize_invite_role(body.role)
    inv = WorkspaceInvite(
        workspace_id=workspace_id,
        email=email,
        role=invite_role,
        token=raw,
        invited_by_user_id=current_user.id,
        expires_at=_utcnow() + timedelta(days=14),
    )
    db.add(inv)
    log_security_audit_event(
        db,
        action="workspace.invite.create",
        resource_type="workspace_invite",
        resource_id=email,
        actor_user_id=current_user.id,
        actor_type="user",
        workspace_id=workspace_id,
        metadata={"role": invite_role},
    )
    db.commit()
    db.refresh(inv)

    url = _workspace_invite_accept_url(raw)
    inviter_name = current_user.name or current_user.email or "A teammate"
    # Respect an existing user's "Project invites" email preference. The invite
    # record + in-app notification still happen; we just don't email them.
    from app.services.notification_prefs import allows_project_invites

    invitee_opted_out = existing_user is not None and not allows_project_invites(
        db, existing_user.id
    )
    if invitee_opted_out:
        email_sent = False
    else:
        email_sent = send_workspace_invite_email(
            to_email=email,
            workspace_name=ws.name,
            inviter_name=inviter_name,
            invite_role=invite_role,
            invite_url=url,
            expires_days=14,
        )
    await _emit_workspace_invite_notification(
        db,
        invite=inv,
        workspace=ws,
        inviter=current_user,
        invited_user=existing_user,
    )
    return WorkspaceInviteCreatedResponse(
        token=raw,
        expires_at=inv.expires_at,
        email_sent=email_sent,
    )


@router.post("/{workspace_id}/invites/{invite_id}/resend")
async def resend_workspace_invite(
    workspace_id: int,
    invite_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = _workspace_or_404(db, workspace_id)
    wm = _require_workspace_member(db, workspace_id, current_user)
    if not can_manage_workspace_members(wm.role):
        raise HTTPException(status_code=403, detail="Not allowed")
    inv = (
        db.query(WorkspaceInvite)
        .filter(
            WorkspaceInvite.id == invite_id,
            WorkspaceInvite.workspace_id == workspace_id,
        )
        .first()
    )
    if not inv:
        raise HTTPException(status_code=404, detail="Invite not found")
    if inv.accepted_at is not None:
        raise HTTPException(status_code=400, detail="Invite was already accepted")
    now = _utcnow()
    exp = inv.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp < now:
        raise HTTPException(status_code=410, detail="Invite has expired; create a new invite")
    remaining_days = max(1, math.ceil((exp - now).total_seconds() / 86400.0))
    url = _workspace_invite_accept_url(inv.token)
    inviter_name = current_user.name or current_user.email or "A teammate"
    email_sent = send_workspace_invite_email(
        to_email=inv.email,
        workspace_name=ws.name,
        inviter_name=inviter_name,
        invite_role=inv.role,
        invite_url=url,
        expires_days=remaining_days,
    )
    invited_user = db.query(User).filter(func.lower(User.email) == inv.email).first()
    await _emit_workspace_invite_notification(
        db,
        invite=inv,
        workspace=ws,
        inviter=current_user,
        invited_user=invited_user,
    )
    return {"email_sent": email_sent}


@router.delete("/{workspace_id}/invites/{invite_id}")
def revoke_workspace_invite(
    workspace_id: int,
    invite_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _workspace_or_404(db, workspace_id)
    wm = _require_workspace_member(db, workspace_id, current_user)
    if not can_manage_workspace_members(wm.role):
        raise HTTPException(status_code=403, detail="Not allowed")
    inv = (
        db.query(WorkspaceInvite)
        .filter(
            WorkspaceInvite.id == invite_id,
            WorkspaceInvite.workspace_id == workspace_id,
        )
        .first()
    )
    if not inv:
        raise HTTPException(status_code=404, detail="Invite not found")
    if inv.accepted_at is not None:
        raise HTTPException(status_code=400, detail="Cannot revoke an invite that was already accepted")
    db.delete(inv)
    db.commit()
    return {"ok": True}


def _mark_workspace_invite_notifications_read(db: Session, user_id: int, inv: WorkspaceInvite) -> None:
    db.query(Notification).filter(
        Notification.user_id == user_id,
        Notification.type == "workspace_invite",
        or_(
            Notification.workspace_invite_id == inv.id,
            Notification.invite_token == inv.token,
        ),
    ).update({Notification.read: True}, synchronize_session=False)


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
        _mark_workspace_invite_notifications_read(db, current_user.id, inv)
        db.commit()
        return {"ok": True, "workspace_id": inv.workspace_id}
    member_role = normalize_invite_role(inv.role)
    db.add(
        WorkspaceMember(
            workspace_id=inv.workspace_id,
            user_id=current_user.id,
            role=member_role,
        )
    )
    inv.accepted_at = _utcnow()
    _mark_workspace_invite_notifications_read(db, current_user.id, inv)
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


@router.post(
    "/{workspace_id}/members/provision",
    response_model=WorkspaceProvisionMemberResponse,
)
def provision_workspace_member(
    workspace_id: int,
    body: WorkspaceProvisionMemberBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Only the workspace owner may create a password account or add an existing user here."""
    ws = _workspace_or_404(db, workspace_id)
    if ws.owner_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the workspace owner can provision accounts")
    email = body.email.strip().lower()
    if not email or "@" not in email or len(email) < 4:
        raise HTTPException(status_code=400, detail="Invalid email address")
    member_role = normalize_invite_role(body.role)
    inviter_name = current_user.name or current_user.email or "Workspace owner"
    login_url = f"{_public_app_base()}/login"

    existing = db.query(User).filter(func.lower(User.email) == email).first()
    if existing:
        if not existing.hashed_password:
            raise HTTPException(
                status_code=400,
                detail="This email uses Google sign-in. Add them with an invite instead, or use another email.",
            )
        already = (
            db.query(WorkspaceMember)
            .filter(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == existing.id,
            )
            .first()
        )
        if already:
            raise HTTPException(status_code=400, detail="That user is already a member of this workspace")
        db.add(
            WorkspaceMember(
                workspace_id=workspace_id,
                user_id=existing.id,
                role=member_role,
            )
        )
        db.commit()
        return WorkspaceProvisionMemberResponse(
            created_new_user=False,
            email=email,
            workspace_role=member_role,
            temporary_password=None,
            email_sent=False,
            detail="Existing account linked to this workspace. Share access separately—they already have a password.",
        )

    display_name = (body.name or "").strip() or email.split("@")[0]
    provided_password = (body.password or "").strip()
    if provided_password:
        password_bytes_len = len(provided_password.encode("utf-8"))
        if password_bytes_len < 8:
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
        if password_bytes_len > 72:
            raise HTTPException(status_code=400, detail="Password cannot exceed 72 bytes")
    temp_password = provided_password or secrets.token_urlsafe(14)
    db_user = User(
        email=email,
        hashed_password=get_password_hash(temp_password),
        name=display_name,
        role="user",
    )
    db.add(db_user)
    db.flush()
    ensure_personal_workspace(db, db_user)
    db.add(
        WorkspaceMember(
            workspace_id=workspace_id,
            user_id=db_user.id,
            role=member_role,
        )
    )
    db.commit()

    email_sent = send_workspace_provisioned_account_email(
        to_email=email,
        display_name=display_name,
        workspace_name=ws.name,
        inviter_name=inviter_name,
        login_url=login_url,
        temporary_password=temp_password,
        workspace_role=member_role,
    )
    return WorkspaceProvisionMemberResponse(
        created_new_user=True,
        email=email,
        workspace_role=member_role,
        temporary_password=temp_password,
        email_sent=email_sent,
        detail=None if email_sent else "Account created; email not sent (configure SMTP). Copy the password below.",
    )


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


@router.get("/{workspace_id}/assets/{asset_id}/media")
def get_workspace_asset_media(
    workspace_id: int,
    asset_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Serve binary asset for members (e.g. thumbnails in the library UI)."""
    _workspace_or_404(db, workspace_id)
    _require_workspace_member(db, workspace_id, current_user)
    a = (
        db.query(WorkspaceAsset)
        .filter(WorkspaceAsset.id == asset_id, WorkspaceAsset.workspace_id == workspace_id)
        .first()
    )
    if not a:
        raise HTTPException(status_code=404, detail="Asset not found")
    path = a.file_url
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")
    media_type, _ = mimetypes.guess_type(path)
    return FileResponse(path, media_type=media_type or "application/octet-stream")


@router.patch("/{workspace_id}/assets/{asset_id}")
def patch_workspace_asset(
    workspace_id: int,
    asset_id: int,
    body: WorkspaceAssetUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _workspace_or_404(db, workspace_id)
    wm = _require_workspace_member(db, workspace_id, current_user)
    if wm.role not in ("owner", "producer", "editor"):
        raise HTTPException(status_code=403, detail="Not allowed")
    if body.title is None and body.category is None:
        raise HTTPException(status_code=400, detail="No fields to update")
    a = (
        db.query(WorkspaceAsset)
        .filter(WorkspaceAsset.id == asset_id, WorkspaceAsset.workspace_id == workspace_id)
        .first()
    )
    if not a:
        raise HTTPException(status_code=404, detail="Asset not found")
    if body.title is not None:
        t = body.title.strip()
        a.title = t if t else "Untitled"
    if body.category is not None:
        c = body.category.strip().lower()
        if c not in ALLOWED_ASSET_CATEGORIES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid category; allowed: {', '.join(sorted(ALLOWED_ASSET_CATEGORIES))}",
            )
        a.category = c
    db.commit()
    db.refresh(a)
    return {
        "id": a.id,
        "category": a.category,
        "title": a.title,
        "file_url": a.file_url,
        "extra": a.extra,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


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


def _workspace_branding_response(db: Session, workspace_id: int, wm: WorkspaceMember) -> WorkspaceBrandingResponse:
    b = (
        db.query(WorkspaceBranding)
        .filter(WorkspaceBranding.workspace_id == workspace_id)
        .first()
    )
    show_token = can_edit_workspace_branding(wm.role)
    if not b:
        return WorkspaceBrandingResponse()
    return WorkspaceBrandingResponse(
        logo_url=b.logo_url,
        primary_color=b.primary_color,
        accent_color=b.accent_color,
        client_footer_text=b.client_footer_text,
        custom_domain=b.custom_domain,
        domain_verified_at=b.domain_verified_at,
        domain_verification_token=(b.domain_verification_token if show_token else None),
    )


@router.get("/{workspace_id}/branding", response_model=WorkspaceBrandingResponse)
def get_workspace_branding(
    workspace_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _workspace_or_404(db, workspace_id)
    wm = _require_workspace_member(db, workspace_id, current_user)
    return _workspace_branding_response(db, workspace_id, wm)


@router.patch("/{workspace_id}/branding", response_model=WorkspaceBrandingResponse)
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
    return _workspace_branding_response(db, workspace_id, wm)


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
    cd = (b.custom_domain or "").strip().lower().rstrip(".")
    record_example = f"_editube-verify.{cd}" if cd else "_editube-verify.your-subdomain.example.com"
    return {
        "verification_token": tok,
        "instructions": (
            f"Add a TXT record at DNS name {record_example} whose value is exactly this token. "
            "After propagation, click Verify DNS."
        ),
    }


@router.post("/{workspace_id}/branding/verify-domain-dns")
def verify_domain_dns(
    workspace_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Checks public DNS for TXT at _editube-verify.<custom_domain>; sets domain_verified_at on success."""

    _workspace_or_404(db, workspace_id)
    wm = _require_workspace_member(db, workspace_id, current_user)
    if not can_edit_workspace_branding(wm.role):
        raise HTTPException(status_code=403, detail="Not allowed")
    b = (
        db.query(WorkspaceBranding)
        .filter(WorkspaceBranding.workspace_id == workspace_id)
        .first()
    )
    if not b or not (b.custom_domain or "").strip():
        raise HTTPException(status_code=400, detail="Save a custom domain on this workspace first.")
    if not (b.domain_verification_token or "").strip():
        raise HTTPException(
            status_code=400,
            detail="Generate a verification token first, add the TXT record at DNS, then try again.",
        )

    ok, msg = verify_editube_domain_txt(b.custom_domain, b.domain_verification_token)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)

    b.domain_verified_at = _utcnow()
    db.commit()
    db.refresh(b)
    return {
        "ok": True,
        "domain_verified_at": b.domain_verified_at.isoformat(),
        "detail": msg,
    }


@router.post("/{workspace_id}/branding/confirm-domain")
def confirm_domain(
    workspace_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Marks domain verified without DNS check. Disabled when EDITUBE_ALLOW_MANUAL_DOMAIN_CONFIRM is not truthy."""

    manual_ok = (
        os.getenv("EDITUBE_ALLOW_MANUAL_DOMAIN_CONFIRM", "1").strip().lower()
        in ("1", "true", "yes", "on")
    )
    if not manual_ok:
        raise HTTPException(
            status_code=403,
            detail="Manual domain confirmation is disabled. Use Verify DNS instead.",
        )

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
        if m.role in ("client", "guest"):
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


@router.get("/{workspace_id}/auth-policy", response_model=WorkspaceAuthPolicyResponse)
def get_workspace_auth_policy(
    workspace_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    wm = _require_workspace_member(db, workspace_id, current_user)
    if wm.role not in ("owner", "producer"):
        raise HTTPException(status_code=403, detail="Not allowed")
    policy = (
        db.query(WorkspaceAuthPolicy)
        .filter(WorkspaceAuthPolicy.workspace_id == workspace_id)
        .first()
    )
    if not policy:
        policy = WorkspaceAuthPolicy(workspace_id=workspace_id, allowed_login_methods=["password", "google"])
        db.add(policy)
        db.commit()
        db.refresh(policy)
    return WorkspaceAuthPolicyResponse(
        workspace_id=workspace_id,
        enforce_sso=policy.enforce_sso,
        allowed_login_methods=policy.allowed_login_methods or [],
        mfa_required=policy.mfa_required,
    )


@router.patch("/{workspace_id}/auth-policy", response_model=WorkspaceAuthPolicyResponse)
def update_workspace_auth_policy(
    workspace_id: int,
    body: WorkspaceAuthPolicyUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    wm = _require_workspace_member(db, workspace_id, current_user)
    if wm.role not in ("owner", "producer"):
        raise HTTPException(status_code=403, detail="Not allowed")
    policy = (
        db.query(WorkspaceAuthPolicy)
        .filter(WorkspaceAuthPolicy.workspace_id == workspace_id)
        .first()
    )
    if not policy:
        policy = WorkspaceAuthPolicy(workspace_id=workspace_id)
        db.add(policy)
    patch = body.model_dump(exclude_unset=True)
    for key, value in patch.items():
        setattr(policy, key, value)
    log_security_audit_event(
        db,
        action="workspace.auth_policy.update",
        resource_type="workspace",
        resource_id=str(workspace_id),
        actor_user_id=current_user.id,
        actor_type="user",
        workspace_id=workspace_id,
        metadata={"changed_fields": sorted(list(patch.keys()))},
    )
    db.commit()
    db.refresh(policy)
    return WorkspaceAuthPolicyResponse(
        workspace_id=workspace_id,
        enforce_sso=policy.enforce_sso,
        allowed_login_methods=policy.allowed_login_methods or [],
        mfa_required=policy.mfa_required,
    )


@router.get("/{workspace_id}/sso-providers", response_model=List[WorkspaceSSOProviderResponse])
def list_workspace_sso_providers(
    workspace_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    wm = _require_workspace_member(db, workspace_id, current_user)
    if wm.role not in ("owner", "producer"):
        raise HTTPException(status_code=403, detail="Not allowed")
    rows = (
        db.query(WorkspaceSSOProvider)
        .filter(WorkspaceSSOProvider.workspace_id == workspace_id)
        .order_by(WorkspaceSSOProvider.created_at.desc())
        .all()
    )
    return rows


@router.post("/{workspace_id}/sso-providers", response_model=WorkspaceSSOProviderResponse)
def create_workspace_sso_provider(
    workspace_id: int,
    body: WorkspaceSSOProviderCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    wm = _require_workspace_member(db, workspace_id, current_user)
    if wm.role not in ("owner", "producer"):
        raise HTTPException(status_code=403, detail="Not allowed")
    metadata = discover_oidc_metadata(body.issuer)
    row = WorkspaceSSOProvider(
        workspace_id=workspace_id,
        provider=body.provider,
        issuer=body.issuer.rstrip("/"),
        client_id=body.client_id,
        client_secret_encrypted=body.client_secret,
        authorization_endpoint=metadata.get("authorization_endpoint"),
        token_endpoint=metadata.get("token_endpoint"),
        userinfo_endpoint=metadata.get("userinfo_endpoint"),
        jwks_uri=metadata.get("jwks_uri"),
        domain_hint=(body.domain_hint or "").strip().lower() or None,
        enabled=body.enabled,
        created_by_user_id=current_user.id,
    )
    db.add(row)
    log_security_audit_event(
        db,
        action="workspace.sso_provider.create",
        resource_type="workspace_sso_provider",
        resource_id=body.provider,
        actor_user_id=current_user.id,
        actor_type="user",
        workspace_id=workspace_id,
    )
    db.commit()
    db.refresh(row)
    return row


@router.get("/{workspace_id}/nda-documents", response_model=List[NDADocumentResponse])
def list_workspace_nda_documents(
    workspace_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_workspace_member(db, workspace_id, current_user)
    return (
        db.query(NDADocument)
        .filter(NDADocument.workspace_id == workspace_id)
        .order_by(NDADocument.updated_at.desc())
        .all()
    )


@router.post("/{workspace_id}/nda-documents", response_model=NDADocumentResponse)
def create_workspace_nda_document(
    workspace_id: int,
    body: NDADocumentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    wm = _require_workspace_member(db, workspace_id, current_user)
    if wm.role not in ("owner", "producer"):
        raise HTTPException(status_code=403, detail="Not allowed")
    import hashlib

    normalized_body = body.body_markdown.strip()
    row = NDADocument(
        workspace_id=workspace_id,
        name=body.name.strip(),
        version=body.version.strip(),
        body_markdown=normalized_body,
        content_sha256=hashlib.sha256(normalized_body.encode("utf-8")).hexdigest(),
        is_active=body.is_active,
        created_by_user_id=current_user.id,
    )
    db.add(row)
    log_security_audit_event(
        db,
        action="workspace.nda_document.create",
        resource_type="nda_document",
        resource_id=f"{row.name}:{row.version}",
        actor_user_id=current_user.id,
        actor_type="user",
        workspace_id=workspace_id,
    )
    db.commit()
    db.refresh(row)
    return row
