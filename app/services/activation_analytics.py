"""Workspace activation measurement with a concurrency-safe first-value gate."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.models import User, WorkspaceActivation
from app.services.analytics_events import FEATURE_KEYS
from app.services.product_analytics import emit


def record_first_value(
    db: Session,
    *,
    user: User | None = None,
    user_id: int | None = None,
    workspace_id: int,
    feature_key: str,
    resource_type: str | None = None,
    resource_id: int | str | None = None,
) -> WorkspaceActivation | None:
    """Record the workspace's first durable output without racing two workers."""

    if feature_key not in FEATURE_KEYS:
        raise ValueError(f"Unknown activation feature: {feature_key}")
    pending = next(
        (
            row
            for row in db.new
            if isinstance(row, WorkspaceActivation) and row.workspace_id == workspace_id
        ),
        None,
    )
    if pending is not None:
        return None
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        # Transaction-scoped and keyed only to this workspace, so unrelated
        # workers remain concurrent while duplicate first-value writes serialize.
        db.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": 1_900_000_000 + int(workspace_id)},
        )
    existing = (
        db.query(WorkspaceActivation)
        .filter(WorkspaceActivation.workspace_id == workspace_id)
        .first()
    )
    if existing:
        return None

    resolved_user_id = user.id if user is not None else user_id
    row = WorkspaceActivation(
        workspace_id=workspace_id,
        user_id=resolved_user_id,
        feature_key=feature_key,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
    )
    db.add(row)
    emit(
        db,
        "first_value_achieved",
        user=user,
        user_id=resolved_user_id,
        workspace_id=workspace_id,
        properties={
            "feature_key": feature_key,
            "activation_definition": "first_durable_output",
            "resource_type": resource_type,
            "resource_id": resource_id,
            "result": "success",
        },
        event_id=f"activation:workspace:{workspace_id}",
    )
    return row
