from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import Project, User, Video, WorkspaceMember
from app.services.pricing import get_plan_spec


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
    return int(used or 0)


def _workspace_member_count(db: Session, workspace_id: int) -> int:
    count = (
        db.query(func.count(WorkspaceMember.id))
        .filter(WorkspaceMember.workspace_id == workspace_id)
        .scalar()
    )
    return int(count or 0)


def get_workspace_storage_snapshot(
    db: Session, *, user: User, workspace_id: int, incoming_bytes: int = 0
) -> StorageUsageSnapshot:
    spec = get_plan_spec(user.plan)
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
    if not snapshot.over_cap:
        return snapshot

    grace_until = user.storage_grace_until
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if grace_until and grace_until > now:
        return snapshot

    if grace_until is None:
        spec = get_plan_spec(user.plan)
        user.storage_grace_until = now + timedelta(days=spec.grace_days)
        db.commit()
        db.refresh(user)
        return snapshot

    raise ValueError("storage_cap_exceeded")


def workspace_usage_payload(db: Session, *, user: User, workspace_id: int) -> dict:
    snapshot = get_workspace_storage_snapshot(db, user=user, workspace_id=workspace_id)
    members = _workspace_member_count(db, workspace_id)
    spec = get_plan_spec(user.plan)
    return {
        "plan": spec.key,
        "storage_used_bytes": snapshot.used_bytes,
        "storage_cap_bytes": snapshot.cap_bytes,
        "storage_addon_tb_price_usd": spec.storage_addon_tb_price_usd,
        "team_members": members,
        "team_cap": spec.seat_cap,
        "storage_grace_until": user.storage_grace_until.isoformat() if user.storage_grace_until else None,
    }
