from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import SecurityAuditLog, WorkspaceMember, User
from app.utils.security import get_current_user

router = APIRouter(prefix="/security/audit", tags=["Security Audit"])


def _can_view_workspace_security(db: Session, user: User, workspace_id: int) -> bool:
    wm = (
        db.query(WorkspaceMember)
        .filter(WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user.id)
        .first()
    )
    return bool(wm and wm.role in ("owner", "producer"))


@router.get("")
def list_security_audit(
    workspace_id: Optional[int] = Query(default=None),
    project_id: Optional[int] = Query(default=None),
    action: Optional[str] = Query(default=None),
    from_ts: Optional[datetime] = Query(default=None),
    to_ts: Optional[datetime] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=2000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = db.query(SecurityAuditLog)
    if workspace_id is not None:
        if not _can_view_workspace_security(db, current_user, workspace_id):
            raise HTTPException(status_code=403, detail="Not allowed to view this workspace audit log")
        q = q.filter(SecurityAuditLog.workspace_id == workspace_id)
    else:
        my_workspaces = db.query(WorkspaceMember.workspace_id).filter(WorkspaceMember.user_id == current_user.id)
        q = q.filter(SecurityAuditLog.workspace_id.in_(my_workspaces))
    if project_id is not None:
        q = q.filter(SecurityAuditLog.project_id == project_id)
    if action:
        q = q.filter(SecurityAuditLog.action == action)
    if from_ts is not None:
        q = q.filter(SecurityAuditLog.created_at >= from_ts)
    if to_ts is not None:
        q = q.filter(SecurityAuditLog.created_at <= to_ts)
    rows = q.order_by(SecurityAuditLog.created_at.desc()).limit(limit).all()
    return rows


@router.get("/export")
def export_security_audit_csv(
    workspace_id: int,
    from_ts: Optional[datetime] = Query(default=None),
    to_ts: Optional[datetime] = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not _can_view_workspace_security(db, current_user, workspace_id):
        raise HTTPException(status_code=403, detail="Not allowed to export this workspace audit log")
    q = db.query(SecurityAuditLog).filter(SecurityAuditLog.workspace_id == workspace_id)
    if from_ts is not None:
        q = q.filter(SecurityAuditLog.created_at >= from_ts)
    if to_ts is not None:
        q = q.filter(SecurityAuditLog.created_at <= to_ts)
    rows = q.order_by(SecurityAuditLog.created_at.asc()).all()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "created_at",
            "action",
            "resource_type",
            "resource_id",
            "outcome",
            "actor_user_id",
            "actor_type",
            "ip_address",
            "country_code",
            "metadata",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.created_at.isoformat() if row.created_at else "",
                row.action,
                row.resource_type,
                row.resource_id or "",
                row.outcome,
                row.actor_user_id or "",
                row.actor_type,
                row.ip_address or "",
                row.country_code or "",
                row.metadata or {},
            ]
        )
    buffer.seek(0)
    filename = f"security_audit_workspace_{workspace_id}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
