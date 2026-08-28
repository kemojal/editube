from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import (
    Project,
    User,
    Video,
    Workspace,
    WorkspaceAsset,
    WorkspaceInvite,
    WorkspaceMember,
)
from app.services.pricing import get_plan_spec
from app.services.product_analytics import emit_after_commit


def _record_quota_threshold(
    *,
    user_id: int,
    workspace_id: int,
    quota_key: str,
    threshold_percent: int,
    used: int,
    cap: int,
    result: str,
) -> None:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    emit_after_commit(
        "quota_threshold_reached",
        event_id=f"quota:{quota_key}:{workspace_id}:{threshold_percent}:{day}",
        user_id=user_id,
        workspace_id=workspace_id,
        properties={
            "quota_key": quota_key,
            "threshold_percent": threshold_percent,
            "used": used,
            "cap": cap,
            "result": result,
        },
    )


@dataclass(frozen=True)
class StorageUsageSnapshot:
    used_bytes: int
    cap_bytes: int
    projected_bytes: int
    over_cap: bool


def _workspace_usage_bytes(db: Session, workspace_id: int) -> int:
    used = (
        db.query(func.coalesce(func.sum(Video.size_bytes), 0))
        .join(Project, Project.id == Video.project_id)
        .filter(Project.workspace_id == workspace_id)
        .scalar()
    )
    # Shared-library assets (b-roll, music, logos) sit in the same bucket and
    # cost the same money, but used to be invisible to the cap — a workspace
    # could push gigabytes through the library and still read "0 B used".
    library = (
        db.query(func.coalesce(func.sum(WorkspaceAsset.size_bytes), 0))
        .filter(WorkspaceAsset.workspace_id == workspace_id)
        .scalar()
    )
    return int(used or 0) + int(library or 0)


def _workspace_member_count(db: Session, workspace_id: int) -> int:
    count = (
        db.query(func.count(WorkspaceMember.id))
        .filter(WorkspaceMember.workspace_id == workspace_id)
        .scalar()
    )
    return int(count or 0)


def workspace_billing_owner(db: Session, workspace_id: int, *, fallback: User) -> User:
    """The account whose plan pays for this workspace.

    Quotas used to be read off whoever happened to be uploading, which is the
    wrong end of the relationship in both directions: a Free collaborator
    invited into a Pro workspace hit a 10 GB wall inside a 2 TB workspace, and
    — the direction that costs money — a Pro member uploading into someone
    else's Free workspace got that workspace a 2 TB cap.
    """
    ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    if not ws:
        return fallback
    owner = db.query(User).filter(User.id == ws.owner_user_id).first()
    return owner or fallback


def get_workspace_storage_snapshot(
    db: Session, *, user: User, workspace_id: int, incoming_bytes: int = 0
) -> StorageUsageSnapshot:
    owner = workspace_billing_owner(db, workspace_id, fallback=user)
    spec = get_plan_spec(owner.plan)
    used_bytes = _workspace_usage_bytes(db, workspace_id)
    projected = used_bytes + max(incoming_bytes, 0)
    return StorageUsageSnapshot(
        used_bytes=used_bytes,
        cap_bytes=spec.included_storage_bytes,
        projected_bytes=projected,
        over_cap=projected > spec.included_storage_bytes,
    )


def assert_storage_upload_allowed(
    db: Session, *, user: User, workspace_id: int, incoming_bytes: int
) -> StorageUsageSnapshot:
    snapshot = get_workspace_storage_snapshot(
        db, user=user, workspace_id=workspace_id, incoming_bytes=incoming_bytes
    )
    owner = workspace_billing_owner(db, workspace_id, fallback=user)
    utilization = (
        int((snapshot.projected_bytes / snapshot.cap_bytes) * 100)
        if snapshot.cap_bytes > 0
        else 100
    )
    threshold = 100 if utilization >= 100 else 90 if utilization >= 90 else 80 if utilization >= 80 else 0
    if threshold:
        _record_quota_threshold(
            user_id=owner.id,
            workspace_id=workspace_id,
            quota_key="storage_bytes",
            threshold_percent=threshold,
            used=snapshot.projected_bytes,
            cap=snapshot.cap_bytes,
            result="blocked" if snapshot.over_cap else "warning",
        )
    if not snapshot.over_cap:
        return snapshot

    # The grace window belongs to the account being billed, not to whichever
    # member tripped the cap — otherwise every new collaborator brings a fresh
    # one and the cap never actually bites.
    grace_until = owner.storage_grace_until
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if grace_until and grace_until > now:
        return snapshot

    if grace_until is None:
        spec = get_plan_spec(owner.plan)
        owner.storage_grace_until = now + timedelta(days=spec.grace_days)
        db.commit()
        db.refresh(owner)
        return snapshot

    raise ValueError("storage_cap_exceeded")


class SeatCapExceeded(Exception):
    """Raised when adding a member would exceed the workspace owner's seat cap."""

    def __init__(self, *, cap: int, used: int) -> None:
        super().__init__("seat_cap_exceeded")
        self.cap = cap
        self.used = used


def workspace_seat_usage(db: Session, workspace_id: int) -> tuple[int, int | None]:
    """(seats consumed, cap) for a workspace, cap None meaning unlimited.

    Pending invites count. They are seats the owner has already promised, and
    without counting them the cap is trivially bypassed by sending N invites
    at once and letting them all be accepted.
    """
    ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    if not ws:
        return 0, None
    owner = db.query(User).filter(User.id == ws.owner_user_id).first()
    cap = get_plan_spec(owner.plan if owner else None).seat_cap

    members = _workspace_member_count(db, workspace_id)
    pending = (
        db.query(func.count(WorkspaceInvite.id))
        .filter(
            WorkspaceInvite.workspace_id == workspace_id,
            WorkspaceInvite.accepted_at.is_(None),
            WorkspaceInvite.expires_at > datetime.now(timezone.utc).replace(tzinfo=None),
        )
        .scalar()
    )
    return members + int(pending or 0), cap


def assert_seat_available(db: Session, workspace_id: int, *, adding: int = 1) -> None:
    """Refuse to add `adding` more people than the owner's plan allows.

    `seat_cap` was advertised in `/billing/catalog` and rendered as a usage
    meter in account settings, but nothing ever checked it — the Free tier's
    "3 seats" was a suggestion, and a free workspace could hold a hundred
    collaborators.
    """
    used, cap = workspace_seat_usage(db, workspace_id)
    if cap is None:
        return
    if used + max(adding, 0) > cap:
        ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
        if ws is not None:
            _record_quota_threshold(
                user_id=ws.owner_user_id,
                workspace_id=workspace_id,
                quota_key="workspace_seats",
                threshold_percent=100,
                used=used + max(adding, 0),
                cap=cap,
                result="blocked",
            )
        raise SeatCapExceeded(cap=cap, used=used)


def workspace_usage_payload(db: Session, *, user: User, workspace_id: int) -> dict:
    snapshot = get_workspace_storage_snapshot(db, user=user, workspace_id=workspace_id)
    owner = workspace_billing_owner(db, workspace_id, fallback=user)
    spec = get_plan_spec(owner.plan)
    # Same number the seat check enforces on, pending invites included —
    # a meter reading "2 / 3" while the third invite is being refused is worse
    # than no meter.
    seats_used, seat_cap = workspace_seat_usage(db, workspace_id)
    return {
        "plan": spec.key,
        "storage_used_bytes": snapshot.used_bytes,
        "storage_cap_bytes": snapshot.cap_bytes,
        "storage_addon_tb_price_usd": spec.storage_addon_tb_price_usd,
        "team_members": seats_used,
        "team_cap": seat_cap,
        "storage_grace_until": (
            owner.storage_grace_until.isoformat() if owner.storage_grace_until else None
        ),
    }
