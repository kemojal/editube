"""Normalized analytics for user-requested background-job cancellation."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import Project, User
from app.services.product_analytics import emit_once


def record_job_canceled(
    db: Session,
    *,
    job_kind: str,
    job_id: int | str,
    feature_key: str,
    user: User,
    project: Project | None = None,
) -> None:
    safe_kind = job_kind.strip().lower().replace(" ", "_")[:80]
    common = {
        "user": user,
        "workspace_id": project.workspace_id if project else None,
        "properties": {
            "job_kind": safe_kind,
            "job_id": str(job_id),
            "feature_key": feature_key,
            "project_id": project.id if project else None,
            "result": "canceled",
        },
    }
    emit_once(
        db,
        "job_canceled",
        event_id=f"job:{safe_kind}:{job_id}:canceled",
        **common,
    )
    emit_once(
        db,
        "feature_canceled",
        event_id=f"feature:{feature_key}:job:{safe_kind}:{job_id}:canceled",
        **common,
    )
