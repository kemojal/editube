from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db.models import SecurityAuditLog


def log_security_audit_event(
    db: Session,
    *,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    actor_user_id: int | None = None,
    actor_type: str = "system",
    outcome: str = "success",
    workspace_id: int | None = None,
    project_id: int | None = None,
    video_id: int | None = None,
    review_link_id: int | None = None,
    session_id: int | None = None,
    ip_address: str | None = None,
    country_code: str | None = None,
    user_agent: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> SecurityAuditLog:
    row = SecurityAuditLog(
        workspace_id=workspace_id,
        project_id=project_id,
        video_id=video_id,
        review_link_id=review_link_id,
        session_id=session_id,
        actor_user_id=actor_user_id,
        actor_type=actor_type,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=outcome,
        ip_address=ip_address,
        country_code=country_code,
        user_agent=user_agent,
        meta_info=metadata or {},
    )
    db.add(row)
    db.flush()
    return row
