"""First-party analytics quality checks and source reconciliation."""

from __future__ import annotations

import os
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import (
    AnalyticsOutbox,
    Project,
    ReviewSession,
    SubscriptionLifecycleEvent,
    User,
    VideoTranscription,
    WorkspaceActivation,
)
from app.services.analytics_events import EVENT_SOURCES, FEATURE_KEYS, SERVER_EVENT_NAMES
from app.services.subscription_analytics import subscription_analytics_event_id


CRITICAL_REQUIRED_PROPERTIES: dict[str, tuple[str, ...]] = {
    "checkout_session_created": ("plan", "recurring_interval", "checkout_attempt_id"),
    "checkout_abandoned": ("plan", "recurring_interval", "maturity_hours", "result"),
    "checkout_completed": ("plan",),
    "feature_started": ("feature_key",),
    "feature_opened": ("feature_key",),
    "feature_completed": ("feature_key",),
    "feature_failed": ("feature_key",),
    "feature_canceled": ("feature_key",),
    "feature_result_used": ("feature_key",),
    "review_playback_milestone_reached": (
        "feature_key",
        "review_session_id",
        "milestone_percent",
    ),
    "job_started": ("job_id", "job_type"),
    "job_completed": ("job_id", "job_type", "result"),
    "job_failed": ("job_id", "job_type", "error_code"),
}


def _expected_environment() -> str:
    return (
        os.getenv("APP_ENV")
        or os.getenv("ENVIRONMENT")
        or ("production" if os.getenv("RENDER") or os.getenv("DOKPLOY") else "local")
    ).strip().lower()


def _issue(
    issues: list[dict[str, Any]],
    severity: str,
    code: str,
    message: str,
    *,
    value: int | float | str | None = None,
    threshold: int | float | str | None = None,
) -> None:
    issues.append(
        {
            "severity": severity,
            "code": code,
            "message": message,
            "value": value,
            "threshold": threshold,
        }
    )


def _event_count(db: Session, event_name: str, since: datetime) -> int:
    return int(
        db.query(func.count(AnalyticsOutbox.event_id))
        .filter(
            AnalyticsOutbox.event_name == event_name,
            AnalyticsOutbox.occurred_at >= since,
        )
        .scalar()
        or 0
    )


def build_analytics_quality_report(
    db: Session,
    *,
    now: datetime | None = None,
    window_hours: int = 24,
) -> dict[str, Any]:
    now = now or datetime.utcnow()
    window_hours = max(1, min(int(window_hours), 24 * 31))
    since = now - timedelta(hours=window_hours)
    issues: list[dict[str, Any]] = []

    rows = (
        db.query(AnalyticsOutbox)
        .filter(AnalyticsOutbox.occurred_at >= since)
        .order_by(AnalyticsOutbox.occurred_at.desc())
        .limit(20_000)
        .all()
    )
    status_counts = Counter(row.delivery_status for row in rows)
    event_counts = Counter(row.event_name for row in rows)

    dead_letters = int(status_counts.get("dead_letter", 0))
    if dead_letters:
        _issue(
            issues,
            "critical",
            "outbox_dead_letters",
            "Analytics events exhausted provider delivery retries.",
            value=dead_letters,
            threshold=0,
        )

    oldest_pending = (
        db.query(func.min(AnalyticsOutbox.created_at))
        .filter(AnalyticsOutbox.delivery_status.in_(("pending", "failed")))
        .scalar()
    )
    backlog_age_minutes: float | None = None
    if oldest_pending is not None:
        backlog_age_minutes = max(0.0, (now - oldest_pending).total_seconds() / 60)
        if backlog_age_minutes > 15:
            _issue(
                issues,
                "critical",
                "outbox_backlog_age",
                "Oldest undelivered analytics event exceeds the delivery SLO.",
                value=round(backlog_age_minutes, 1),
                threshold=15,
            )
        elif backlog_age_minutes > 5:
            _issue(
                issues,
                "warning",
                "outbox_backlog_age",
                "Analytics delivery is approaching its backlog SLO.",
                value=round(backlog_age_minutes, 1),
                threshold=5,
            )

    malformed = 0
    unknown_events: set[str] = set()
    unknown_sources: set[str] = set()
    unknown_features: set[str] = set()
    route_templates: set[str] = set()
    for row in rows:
        properties = row.properties if isinstance(row.properties, dict) else {}
        if row.event_name not in SERVER_EVENT_NAMES:
            unknown_events.add(row.event_name)
        if row.source not in EVENT_SOURCES:
            unknown_sources.add(row.source)
        feature_key = properties.get("feature_key")
        if feature_key is not None and feature_key not in FEATURE_KEYS:
            unknown_features.add(str(feature_key))
        required = CRITICAL_REQUIRED_PROPERTIES.get(row.event_name, ())
        if any(properties.get(key) in (None, "") for key in required):
            malformed += 1
        route = properties.get("path_template") or properties.get("route_template")
        if isinstance(route, str):
            route_templates.add(route)

    if malformed:
        _issue(
            issues,
            "critical",
            "missing_required_properties",
            "Critical analytics events are missing required dimensions.",
            value=malformed,
            threshold=0,
        )
    for code, values in (
        ("unknown_event_names", unknown_events),
        ("unknown_event_sources", unknown_sources),
        ("unknown_feature_keys", unknown_features),
    ):
        if values:
            _issue(
                issues,
                "critical",
                code,
                "Unregistered analytics schema values were persisted.",
                value=", ".join(sorted(values)[:20]),
                threshold=0,
            )

    environments = Counter(row.environment for row in rows)
    wrong_environment = sum(
        count for environment, count in environments.items() if environment != _expected_environment()
    )
    if wrong_environment:
        _issue(
            issues,
            "critical",
            "environment_contamination",
            "Events from another environment are present in the current window.",
            value=wrong_environment,
            threshold=0,
        )

    if len(rows) >= 100 and len(route_templates) / len(rows) > 0.5:
        _issue(
            issues,
            "warning",
            "route_cardinality_spike",
            "Route-template cardinality suggests raw identifiers may be leaking.",
            value=round(len(route_templates) / len(rows), 3),
            threshold=0.5,
        )

    current_hour = now - timedelta(hours=1)
    previous_hour = now - timedelta(hours=2)
    current_volume = int(
        db.query(func.count(AnalyticsOutbox.event_id))
        .filter(AnalyticsOutbox.occurred_at >= current_hour)
        .scalar()
        or 0
    )
    previous_volume = int(
        db.query(func.count(AnalyticsOutbox.event_id))
        .filter(
            AnalyticsOutbox.occurred_at >= previous_hour,
            AnalyticsOutbox.occurred_at < current_hour,
        )
        .scalar()
        or 0
    )
    if previous_volume >= 20:
        ratio = current_volume / previous_volume
        if ratio < 0.5 or ratio > 3:
            _issue(
                issues,
                "warning",
                "event_volume_anomaly",
                "Hourly analytics volume moved outside the baseline band.",
                value=round(ratio, 3),
                threshold="0.5..3.0",
            )

    previous_day_start = since - timedelta(hours=window_hours)
    previous_day_volume = int(
        db.query(func.count(AnalyticsOutbox.event_id))
        .filter(
            AnalyticsOutbox.occurred_at >= previous_day_start,
            AnalyticsOutbox.occurred_at < since,
        )
        .scalar()
        or 0
    )
    if previous_day_volume >= 20:
        day_ratio = len(rows) / previous_day_volume
        if day_ratio < 0.5 or day_ratio > 1.5:
            _issue(
                issues,
                "warning",
                "day_over_day_volume_anomaly",
                "Analytics volume changed by more than 50% versus the prior window.",
                value=round(day_ratio, 3),
                threshold="0.5..1.5",
            )

    reconciliations: dict[str, dict[str, int]] = {}
    for name, model, event_name, timestamp in (
        ("accounts", User, "account_created", User.created_at),
        ("projects", Project, "project_created", Project.created_at),
        (
            "workspace_activations",
            WorkspaceActivation,
            "first_value_achieved",
            WorkspaceActivation.achieved_at,
        ),
        (
            "completed_transcriptions",
            VideoTranscription,
            "transcription_completed",
            VideoTranscription.updated_at,
        ),
    ):
        source_query = db.query(func.count(model.id)).filter(timestamp >= since)
        if model is VideoTranscription:
            source_query = source_query.filter(VideoTranscription.status == "completed")
        source_count = int(source_query.scalar() or 0)
        analytics_count = _event_count(db, event_name, since)
        reconciliations[name] = {
            "source_count": source_count,
            "analytics_count": analytics_count,
            "missing_count": max(0, source_count - analytics_count),
        }
        if source_count > analytics_count:
            _issue(
                issues,
                "warning",
                f"{name}_reconciliation_gap",
                f"{name.replace('_', ' ').title()} exceed matching analytics events.",
                value=source_count - analytics_count,
                threshold=0,
            )

    lifecycle_rows = (
        db.query(SubscriptionLifecycleEvent.event_key)
        .filter(SubscriptionLifecycleEvent.occurred_at >= since)
        .all()
    )
    expected_subscription_ids = {
        subscription_analytics_event_id(event_key) for (event_key,) in lifecycle_rows
    }
    existing_subscription_ids = {
        event_id
        for (event_id,) in db.query(AnalyticsOutbox.event_id)
        .filter(AnalyticsOutbox.event_id.in_(expected_subscription_ids or {"none"}))
        .all()
    }
    missing_subscriptions = len(expected_subscription_ids - existing_subscription_ids)
    reconciliations["subscription_lifecycle"] = {
        "source_count": len(expected_subscription_ids),
        "analytics_count": len(existing_subscription_ids),
        "missing_count": missing_subscriptions,
    }
    if missing_subscriptions:
        _issue(
            issues,
            "critical",
            "subscription_reconciliation_gap",
            "Subscription lifecycle facts are missing matching outbox events.",
            value=missing_subscriptions,
            threshold=0,
        )

    milestone_sessions = (
        db.query(ReviewSession.id, ReviewSession.analytics_milestones)
        .filter(ReviewSession.analytics_milestones.isnot(None))
        .limit(10_000)
        .all()
    )
    expected_milestone_ids = {
        f"review-milestone:{session_id}:{int(milestone)}"
        for session_id, milestones in milestone_sessions
        for milestone in (milestones or [])
    }
    existing_milestone_ids = {
        event_id
        for (event_id,) in db.query(AnalyticsOutbox.event_id)
        .filter(AnalyticsOutbox.event_id.in_(expected_milestone_ids or {"none"}))
        .all()
    }
    missing_milestones = len(expected_milestone_ids - existing_milestone_ids)
    reconciliations["review_milestones"] = {
        "source_count": len(expected_milestone_ids),
        "analytics_count": len(existing_milestone_ids),
        "missing_count": missing_milestones,
    }
    if missing_milestones:
        _issue(
            issues,
            "critical",
            "review_milestone_reconciliation_gap",
            "Review milestone ledgers are missing matching outbox events.",
            value=missing_milestones,
            threshold=0,
        )

    overall_status = (
        "critical"
        if any(item["severity"] == "critical" for item in issues)
        else "degraded" if issues else "healthy"
    )
    return {
        "status": overall_status,
        "generated_at": now,
        "window_hours": window_hours,
        "metrics": {
            "event_count": len(rows),
            "event_count_truncated": len(rows) == 20_000,
            "events_by_name": dict(sorted(event_counts.items())),
            "delivery_status_counts": dict(sorted(status_counts.items())),
            "oldest_backlog_age_minutes": (
                round(backlog_age_minutes, 1) if backlog_age_minutes is not None else None
            ),
            "current_hour_volume": current_volume,
            "previous_hour_volume": previous_volume,
            "previous_window_volume": previous_day_volume,
            "authoritative_duplicate_count": 0,
            "route_template_cardinality": len(route_templates),
        },
        "reconciliation": reconciliations,
        "issues": issues,
    }
