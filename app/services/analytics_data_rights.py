"""First-party analytics export/deletion primitives shared by API and account closure."""

from __future__ import annotations

from datetime import datetime
import hashlib
import os

from sqlalchemy.orm import Session

from app.db.models import (
    AnalyticsConsent,
    AnalyticsConsentEvent,
    AnalyticsDataRequest,
    AnalyticsFeedback,
    AnalyticsOutbox,
    SubscriptionLifecycleEvent,
    WorkspaceActivation,
)
from app.services.checkout_analytics import anonymize_checkout_attempts


_IDENTIFIER_PROPERTY_KEYS = {
    "analytics_session_id",
    "request_id",
    "trace_id",
    "job_id",
    "resource_id",
    "stripe_invoice_id",
    "stripe_subscription_id",
}


def request_analytics_deletion(
    db: Session,
    *,
    user_id: int,
    distinct_id: str | None = None,
) -> AnalyticsDataRequest:
    """Delete restricted rows and anonymize historical aggregate facts.

    The provider deletion remains asynchronous. The returned row is the audit
    handle used by the delivery worker; callers commit it with their own state.
    """

    posthog_configured = any(
        (os.getenv(name) or "").strip()
        for name in (
            "POSTHOG_PROJECT_API_KEY",
            "POSTHOG_PROJECT_ID",
            "POSTHOG_PERSONAL_API_KEY",
        )
    )
    deletion_credentials_ready = bool(
        (os.getenv("POSTHOG_PROJECT_ID") or "").strip()
        and (os.getenv("POSTHOG_PERSONAL_API_KEY") or "").strip()
    )
    request_row = AnalyticsDataRequest(
        user_id=user_id,
        distinct_id=(
            (distinct_id or str(user_id))
            if posthog_configured
            else f"deleted:{hashlib.sha256((distinct_id or str(user_id)).encode()).hexdigest()}"
        ),
        request_type="delete",
        status=(
            "pending"
            if deletion_credentials_ready
            else "pending_configuration" if posthog_configured else "completed"
        ),
        provider_status={
            "posthog": (
                "pending"
                if deletion_credentials_ready
                else "pending_configuration" if posthog_configured else "not_configured"
            ),
            "sentry": "retention_policy",
            "first_party": "deleted",
        },
        completed_at=None if posthog_configured else datetime.utcnow(),
    )
    if not posthog_configured:
        request_row.user_id = None
    db.add(request_row)
    db.query(AnalyticsConsent).filter(AnalyticsConsent.user_id == user_id).delete(
        synchronize_session=False
    )
    db.query(AnalyticsConsentEvent).filter(
        AnalyticsConsentEvent.user_id == user_id
    ).delete(synchronize_session=False)
    db.query(AnalyticsFeedback).filter(AnalyticsFeedback.user_id == user_id).delete(
        synchronize_session=False
    )
    anonymize_checkout_attempts(db, user_id=user_id)

    for row in db.query(AnalyticsOutbox).filter(AnalyticsOutbox.user_id == user_id).all():
        if row.delivery_status in {"pending", "failed", "delivering"}:
            # A privacy request must not turn an identifiable queued event into
            # a newly delivered synthetic person. Already delivered/in-flight
            # provider data is handled by the asynchronous provider deletion.
            row.delivery_status = "suppressed"
            row.delivery_started_at = None
            row.next_attempt_at = None
            row.last_error_code = "privacy_deletion"
        row.user_id = None
        row.workspace_id = None
        row.anonymous_id = None
        row.properties = {
            key: value
            for key, value in (row.properties or {}).items()
            if key not in _IDENTIFIER_PROPERTY_KEYS and not key.endswith("_id")
        }

    for row in (
        db.query(SubscriptionLifecycleEvent)
        .filter(SubscriptionLifecycleEvent.user_id == user_id)
        .all()
    ):
        row.event_key = f"deleted:{hashlib.sha256(row.event_key.encode()).hexdigest()}"
        row.source_event_id = None
        row.user_id = None
        row.workspace_id = None
        row.stripe_subscription_id = None
        row.stripe_invoice_id = None
        row.meta_info = None
    db.query(WorkspaceActivation).filter(WorkspaceActivation.user_id == user_id).update(
        {WorkspaceActivation.user_id: None, WorkspaceActivation.resource_id: None},
        synchronize_session=False,
    )
    return request_row
