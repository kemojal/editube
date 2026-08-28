"""Vendor-neutral, transactional product analytics emission."""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.db.database import SessionLocal
from app.db.models import AnalyticsOutbox, User, WorkspaceMember
from app.services.analytics_events import EVENT_SOURCES, FEATURE_KEYS, SERVER_EVENT_NAMES
from app.services.analytics_privacy import sanitize_properties
from app.services.request_context import current_request_context


logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.utcnow()


def _environment() -> str:
    return (
        os.getenv("APP_ENV")
        or os.getenv("ENVIRONMENT")
        or ("production" if os.getenv("RENDER") or os.getenv("DOKPLOY") else "local")
    ).strip().lower()


def workspace_id_for_user(db: Session, user_id: int) -> int | None:
    row = (
        db.query(WorkspaceMember.workspace_id)
        .filter(WorkspaceMember.user_id == user_id)
        .order_by(WorkspaceMember.id.asc())
        .first()
    )
    return int(row[0]) if row else None


def emit(
    db: Session,
    event_name: str,
    *,
    user: User | None = None,
    user_id: int | None = None,
    workspace_id: int | None = None,
    anonymous_id: str | None = None,
    properties: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
    source: str = "api",
    event_id: str | None = None,
    schema_version: int = 1,
) -> AnalyticsOutbox:
    if event_name not in SERVER_EVENT_NAMES:
        raise ValueError(f"Unknown analytics event: {event_name}")
    if source not in EVENT_SOURCES:
        raise ValueError(f"Unknown analytics source: {source}")

    clean = sanitize_properties(properties)
    feature_key = clean.get("feature_key")
    if feature_key is not None and feature_key not in FEATURE_KEYS:
        raise ValueError(f"Unknown analytics feature_key: {feature_key}")

    context = current_request_context()
    resolved_user_id = user.id if user is not None else user_id or context.user_id
    resolved_workspace_id = workspace_id or context.workspace_id
    if resolved_workspace_id is None and resolved_user_id is not None:
        resolved_workspace_id = workspace_id_for_user(db, resolved_user_id)

    standard = {
        "path_template": context.route_template,
        "request_id": context.request_id,
        "trace_id": context.trace_id,
        "analytics_session_id": context.analytics_session_id,
        "plan": getattr(user, "plan", None) if user is not None else context.plan,
        "subscription_status": (
            getattr(user, "subscription_status", None)
            if user is not None
            else context.subscription_status
        ),
        "user_role": getattr(user, "role", None) if user is not None else context.user_role,
    }
    for key, value in standard.items():
        if value is not None and key not in clean:
            clean[key] = value
    clean = sanitize_properties(clean)

    row = AnalyticsOutbox(
        event_id=event_id or str(uuid.uuid4()),
        event_name=event_name,
        schema_version=max(1, int(schema_version)),
        occurred_at=occurred_at or _utcnow(),
        source=source,
        environment=_environment(),
        release=(os.getenv("RELEASE") or os.getenv("GIT_SHA") or "").strip() or None,
        user_id=resolved_user_id,
        workspace_id=resolved_workspace_id,
        anonymous_id=(anonymous_id or "").strip() or None,
        properties=clean,
        delivery_status="pending",
    )
    db.add(row)
    return row


def emit_once(
    db: Session,
    event_name: str,
    *,
    event_id: str,
    **kwargs: Any,
) -> AnalyticsOutbox:
    """Return an existing deterministic event or stage it once in this transaction."""

    normalized_id = event_id.strip()
    if not normalized_id:
        raise ValueError("event_id is required for emit_once")
    # SessionLocal deliberately disables autoflush. Querying alone therefore
    # cannot see an event staged earlier in the same product transaction.
    for pending in db.new:
        if isinstance(pending, AnalyticsOutbox) and pending.event_id == normalized_id:
            if pending.event_name != event_name:
                raise ValueError("event_id already belongs to another analytics event")
            return pending
    existing = (
        db.query(AnalyticsOutbox)
        .filter(AnalyticsOutbox.event_id == normalized_id)
        .first()
    )
    if existing is not None:
        if existing.event_name != event_name:
            raise ValueError("event_id already belongs to another analytics event")
        return existing
    return emit(db, event_name, event_id=normalized_id, **kwargs)


def emit_after_commit(event_name: str, **kwargs: Any) -> str | None:
    """Best-effort bridge for legacy call sites that already committed state."""

    db = SessionLocal()
    try:
        row = emit(db, event_name, **kwargs)
        db.commit()
        return row.event_id
    except IntegrityError:
        # Deterministic event IDs make retries exactly-once at the outbox edge.
        db.rollback()
        event_id = kwargs.get("event_id")
        if event_id and db.query(AnalyticsOutbox.event_id).filter(
            AnalyticsOutbox.event_id == event_id
        ).first():
            return event_id
        logger.exception("Analytics event integrity failure event=%s", event_name)
        return None
    except Exception:
        db.rollback()
        logger.exception("Analytics event emission failed event=%s", event_name)
        return None
    finally:
        db.close()
