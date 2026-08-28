"""Persistent checkout attempts and mature abandonment modeling."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib

from sqlalchemy.orm import Session

from app.db.models import CheckoutAttempt, User
from app.services.product_analytics import emit_once, workspace_id_for_user


def _event_id(attempt_id: int) -> str:
    return f"checkout-attempt:{attempt_id}:abandoned"


def create_checkout_attempt(
    db: Session,
    *,
    user: User,
    stripe_checkout_session_id: str,
    plan: str,
    recurring_interval: str,
    trial_days: int,
    offer_applied: bool,
    campaign_id: str | None,
    source: str = "billing_checkout",
) -> CheckoutAttempt:
    row = CheckoutAttempt(
        user_id=user.id,
        workspace_id=workspace_id_for_user(db, user.id),
        stripe_checkout_session_id=stripe_checkout_session_id,
        plan=plan,
        recurring_interval=recurring_interval,
        campaign_id=campaign_id,
        source=source,
        trial_days=max(0, int(trial_days)),
        offer_applied=bool(offer_applied),
    )
    db.add(row)
    db.flush()
    return row


def complete_checkout_attempt(
    db: Session,
    *,
    stripe_checkout_session_id: str | None,
    completed_at: datetime,
) -> CheckoutAttempt | None:
    if not stripe_checkout_session_id:
        return None
    row = (
        db.query(CheckoutAttempt)
        .filter(CheckoutAttempt.stripe_checkout_session_id == stripe_checkout_session_id)
        .with_for_update()
        .first()
    )
    if row is None:
        return None
    row.status = "completed"
    row.completed_at = row.completed_at or completed_at
    row.canceled_at = None
    row.abandoned_at = None
    return row


def cancel_latest_checkout_attempt(
    db: Session,
    *,
    user_id: int,
    canceled_at: datetime,
) -> CheckoutAttempt | None:
    """Record the explicit Stripe back-button signal for the current user.

    Stripe does not document the success-session placeholder for cancel URLs,
    so the authenticated return page closes the user's newest open attempt.
    Completion still resolves by exact Stripe session ID and wins if it races.
    """

    row = (
        db.query(CheckoutAttempt)
        .filter(
            CheckoutAttempt.user_id == user_id,
            CheckoutAttempt.status == "created",
        )
        .order_by(CheckoutAttempt.created_at.desc(), CheckoutAttempt.id.desc())
        .with_for_update()
        .first()
    )
    if row is None:
        return None
    row.status = "canceled"
    row.canceled_at = canceled_at
    row.abandoned_at = None
    return row


def model_mature_checkout_abandonment(
    db: Session,
    *,
    now: datetime | None = None,
    maturity_hours: int = 24,
    limit: int = 500,
) -> int:
    now = now or datetime.utcnow()
    cutoff = now - timedelta(hours=max(1, int(maturity_hours)))
    rows = (
        db.query(CheckoutAttempt)
        .filter(
            CheckoutAttempt.status == "created",
            CheckoutAttempt.created_at <= cutoff,
        )
        .order_by(CheckoutAttempt.created_at.asc(), CheckoutAttempt.id.asc())
        .with_for_update(skip_locked=True)
        .limit(max(1, min(int(limit), 5000)))
        .all()
    )
    for row in rows:
        row.status = "abandoned"
        row.abandoned_at = now
        emit_once(
            db,
            "checkout_abandoned",
            event_id=_event_id(row.id),
            user_id=row.user_id,
            workspace_id=row.workspace_id,
            occurred_at=now,
            source="modeled",
            properties={
                "plan": row.plan,
                "recurring_interval": row.recurring_interval,
                "source": row.source,
                "trial_offered": row.trial_days > 0,
                "offer_applied": row.offer_applied,
                "campaign": row.campaign_id,
                "maturity_hours": max(1, int(maturity_hours)),
                "result": "abandoned",
            },
        )
    return len(rows)


def anonymize_checkout_attempts(db: Session, *, user_id: int) -> None:
    for row in db.query(CheckoutAttempt).filter(CheckoutAttempt.user_id == user_id).all():
        row.stripe_checkout_session_id = (
            "deleted:" + hashlib.sha256(row.stripe_checkout_session_id.encode()).hexdigest()
        )
        row.user_id = None
        row.workspace_id = None
        row.campaign_id = None
