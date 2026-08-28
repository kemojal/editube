"""First-party product analytics retention with conservative defaults."""

from __future__ import annotations

from datetime import datetime, timedelta
import os

from sqlalchemy.orm import Session

from app.db.models import (
    AnalyticsConsentEvent,
    AnalyticsDataRequest,
    AnalyticsFeedback,
    AnalyticsOutbox,
    CheckoutAttempt,
)


def _days(name: str, default: int, minimum: int) -> int:
    try:
        value = int((os.getenv(name) or str(default)).strip())
    except ValueError:
        value = default
    return max(minimum, value)


def apply_analytics_retention(db: Session, *, now: datetime | None = None) -> dict[str, int]:
    """Delete only records whose operational/legal purpose has expired.

    Pending/retrying outbox rows and subscription lifecycle history are never
    removed here. Provider retention is configured in PostHog/Sentry.
    """

    now = now or datetime.utcnow()
    raw_event_cutoff = now - timedelta(
        days=_days("ANALYTICS_RAW_EVENT_RETENTION_DAYS", 456, 30)
    )
    feedback_cutoff = now - timedelta(
        days=_days("ANALYTICS_FEEDBACK_RETENTION_DAYS", 365, 30)
    )
    audit_cutoff = now - timedelta(
        days=_days("ANALYTICS_AUDIT_RETENTION_DAYS", 760, 90)
    )

    outbox_deleted = (
        db.query(AnalyticsOutbox)
        .filter(
            AnalyticsOutbox.delivery_status.in_(("delivered", "dead_letter", "suppressed")),
            AnalyticsOutbox.occurred_at < raw_event_cutoff,
        )
        .delete(synchronize_session=False)
    )
    feedback_deleted = (
        db.query(AnalyticsFeedback)
        .filter(AnalyticsFeedback.created_at < feedback_cutoff)
        .delete(synchronize_session=False)
    )
    consent_events_deleted = (
        db.query(AnalyticsConsentEvent)
        .filter(AnalyticsConsentEvent.occurred_at < audit_cutoff)
        .delete(synchronize_session=False)
    )
    data_requests_deleted = (
        db.query(AnalyticsDataRequest)
        .filter(
            AnalyticsDataRequest.user_id.is_(None),
            AnalyticsDataRequest.status == "completed",
            AnalyticsDataRequest.completed_at.is_not(None),
            AnalyticsDataRequest.completed_at < audit_cutoff,
        )
        .delete(synchronize_session=False)
    )
    checkout_attempts_deleted = (
        db.query(CheckoutAttempt)
        .filter(
            CheckoutAttempt.status.in_(("completed", "canceled", "abandoned")),
            CheckoutAttempt.created_at < raw_event_cutoff,
        )
        .delete(synchronize_session=False)
    )
    db.commit()
    return {
        "outbox_deleted": int(outbox_deleted or 0),
        "feedback_deleted": int(feedback_deleted or 0),
        "consent_events_deleted": int(consent_events_deleted or 0),
        "data_requests_deleted": int(data_requests_deleted or 0),
        "checkout_attempts_deleted": int(checkout_attempts_deleted or 0),
    }
