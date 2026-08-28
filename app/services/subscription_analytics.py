"""Authoritative, idempotent Stripe lifecycle facts and analytics events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy import text

from app.db.models import Subscription, SubscriptionLifecycleEvent, User
from app.services.analytics_privacy import AnalyticsPrivacyError, encrypt_restricted_comment
from app.services.entitlements import stripe_datetime, stripe_field
from app.services.product_analytics import emit, workspace_id_for_user


logger = logging.getLogger(__name__)
_EVENT_NAMESPACE = uuid.UUID("e2224aa2-2e86-4c3b-9237-a3e7a36fe927")


@dataclass(frozen=True)
class SubscriptionSnapshot:
    plan: str | None
    status: str | None
    cancel_at_period_end: bool
    ended_at: datetime | None


def snapshot_subscription(row: Subscription | None) -> SubscriptionSnapshot:
    return SubscriptionSnapshot(
        plan=row.plan if row else None,
        status=row.status if row else None,
        cancel_at_period_end=bool(row.cancel_at_period_end) if row else False,
        ended_at=row.ended_at if row else None,
    )


def _first_item(subscription: Any) -> Any | None:
    items = stripe_field(subscription, "items")
    data = stripe_field(items, "data") or []
    return data[0] if data else None


def _price(subscription: Any) -> Any | None:
    return stripe_field(_first_item(subscription), "price")


def _discount(subscription: Any) -> Any | None:
    discount = stripe_field(subscription, "discount")
    if discount:
        return discount
    discounts = stripe_field(subscription, "discounts") or []
    return discounts[0] if discounts else None


def sync_subscription_analytics_fields(row: Subscription, subscription: Any) -> None:
    """Mirror the stable financial/cancellation fields required for cohort analysis."""

    item = _first_item(subscription)
    price = _price(subscription)
    recurring = stripe_field(price, "recurring")
    row.currency = (str(stripe_field(price, "currency") or "").lower() or None)
    unit_amount = stripe_field(price, "unit_amount")
    row.unit_amount = int(unit_amount) if unit_amount is not None else None
    quantity = stripe_field(item, "quantity")
    row.quantity = int(quantity) if quantity is not None else 1
    row.recurring_interval = stripe_field(recurring, "interval")

    latest_invoice = stripe_field(subscription, "latest_invoice")
    row.latest_invoice_id = (
        latest_invoice
        if isinstance(latest_invoice, str)
        else stripe_field(latest_invoice, "id")
    )

    discount = _discount(subscription)
    coupon = stripe_field(discount, "coupon")
    amount_off = stripe_field(coupon, "amount_off")
    percent_off = stripe_field(coupon, "percent_off")
    row.discount_amount = int(amount_off) if amount_off is not None else None
    row.discount_percent = float(percent_off) if percent_off is not None else None

    details = stripe_field(subscription, "cancellation_details")
    reason = stripe_field(details, "reason")
    feedback = stripe_field(details, "feedback")
    comment = stripe_field(details, "comment")
    canceled_at = stripe_datetime(stripe_field(subscription, "canceled_at"))
    cancel_at = stripe_datetime(stripe_field(subscription, "cancel_at"))
    row.cancellation_feedback = str(feedback or reason or "").strip() or None
    if canceled_at:
        row.cancellation_requested_at = row.cancellation_requested_at or canceled_at
    if row.cancel_at_period_end:
        row.cancellation_requested_at = row.cancellation_requested_at or datetime.utcnow()
        row.cancellation_effective_at = cancel_at or row.current_period_end
    elif canceled_at:
        row.cancellation_effective_at = canceled_at
    else:
        row.cancellation_effective_at = None
    row.cancellation_source = "stripe" if (reason or feedback or canceled_at) else None
    if reason:
        row.voluntary_churn = str(reason) == "cancellation_requested"
    elif feedback:
        row.voluntary_churn = True
    if comment:
        try:
            row.cancellation_comment_encrypted = encrypt_restricted_comment(str(comment))
        except AnalyticsPrivacyError:
            # Never fall back to plaintext. The normalized Stripe category above
            # remains enough for aggregate churn analysis.
            row.cancellation_comment_encrypted = None
            logger.warning("Cancellation comment omitted because encryption is not configured")


def _naive_utc(value: Any) -> datetime:
    parsed = stripe_datetime(value)
    if parsed:
        return parsed
    return datetime.now(timezone.utc).replace(tzinfo=None)


def subscription_analytics_event_id(event_key: str) -> str:
    return str(uuid.uuid5(_EVENT_NAMESPACE, event_key))


def record_subscription_lifecycle(
    db: Session,
    *,
    event_type: str,
    user: User,
    subscription: Subscription | None = None,
    source_event_id: str | None = None,
    source_object_id: str | None = None,
    previous: SubscriptionSnapshot | None = None,
    invoice_id: str | None = None,
    amount_minor: int | None = None,
    currency: str | None = None,
    reason_code: str | None = None,
    voluntary: bool | None = None,
    effective_at: datetime | None = None,
    occurred_at: Any = None,
    meta_info: dict[str, Any] | None = None,
    event_source: str = "stripe_webhook",
) -> SubscriptionLifecycleEvent | None:
    """Write one lifecycle fact and matching outbox event in the same transaction."""

    stable_source = source_event_id or source_object_id
    if not stable_source:
        stable_source = (
            f"{subscription.stripe_subscription_id}:"
            f"{int(_naive_utc(occurred_at).timestamp())}"
            if subscription
            else str(int(_naive_utc(occurred_at).timestamp()))
        )
    event_key = f"{event_type}:{stable_source}"
    pending = next(
        (
            row
            for row in db.new
            if isinstance(row, SubscriptionLifecycleEvent) and row.event_key == event_key
        ),
        None,
    )
    if pending is not None:
        return None
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        # Stripe can retry the same event against two API replicas at once.
        # Serialize only this event key so the unique constraint never turns a
        # harmless duplicate into a failed webhook transaction.
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:event_key))"),
            {"event_key": event_key},
        )
    existing = (
        db.query(SubscriptionLifecycleEvent)
        .filter(SubscriptionLifecycleEvent.event_key == event_key)
        .first()
    )
    if existing:
        return None

    workspace_id = workspace_id_for_user(db, user.id)
    previous = previous or SubscriptionSnapshot(None, None, False, None)
    occurred = _naive_utc(occurred_at)
    row = SubscriptionLifecycleEvent(
        event_key=event_key,
        event_type=event_type,
        source_event_id=source_event_id,
        user_id=user.id,
        workspace_id=workspace_id,
        stripe_subscription_id=(subscription.stripe_subscription_id if subscription else None),
        stripe_invoice_id=invoice_id,
        plan=(subscription.plan if subscription else user.plan),
        previous_plan=previous.plan,
        status=(subscription.status if subscription else user.subscription_status),
        previous_status=previous.status,
        currency=(currency or (subscription.currency if subscription else None)),
        amount_minor=(amount_minor if amount_minor is not None else (
            subscription.unit_amount * (subscription.quantity or 1)
            if subscription and subscription.unit_amount is not None
            else None
        )),
        quantity=(subscription.quantity if subscription else None),
        recurring_interval=(subscription.recurring_interval if subscription else None),
        voluntary=(voluntary if voluntary is not None else (
            subscription.voluntary_churn if subscription else None
        )),
        reason_code=(reason_code or (subscription.cancellation_feedback if subscription else None)),
        effective_at=effective_at,
        meta_info=meta_info or None,
        occurred_at=occurred,
    )
    db.add(row)

    properties = {
        "plan": row.plan,
        "previous_plan": row.previous_plan,
        "subscription_status": row.status,
        "previous_subscription_status": row.previous_status,
        "currency": row.currency,
        "amount_minor": row.amount_minor,
        "quantity": row.quantity,
        "recurring_interval": row.recurring_interval,
        "voluntary": row.voluntary,
        "reason_code": row.reason_code,
        "stripe_subscription_id": row.stripe_subscription_id,
        "stripe_invoice_id": row.stripe_invoice_id,
    }
    emit(
        db,
        event_type,
        user=user,
        workspace_id=workspace_id,
        properties={key: value for key, value in properties.items() if value is not None},
        occurred_at=occurred,
        source=event_source,
        event_id=subscription_analytics_event_id(event_key),
    )
    return row


def record_subscription_transitions(
    db: Session,
    *,
    user: User,
    subscription: Subscription,
    previous: SubscriptionSnapshot,
    source_event_id: str | None,
    occurred_at: Any = None,
) -> list[str]:
    """Classify all meaningful state changes represented by one Stripe update."""

    events: list[str] = []

    def add(event_type: str, suffix: str = "") -> None:
        source = f"{source_event_id}:{suffix}" if source_event_id and suffix else source_event_id
        created = record_subscription_lifecycle(
            db,
            event_type=event_type,
            user=user,
            subscription=subscription,
            previous=previous,
            source_event_id=source,
            occurred_at=occurred_at,
        )
        if created:
            events.append(event_type)

    if subscription.plan and previous.plan and subscription.plan != previous.plan:
        add("subscription_plan_changed", "plan")

    if not previous.cancel_at_period_end and subscription.cancel_at_period_end:
        add("subscription_cancel_scheduled", "cancel")
    elif previous.cancel_at_period_end and not subscription.cancel_at_period_end:
        add("subscription_cancel_reversed", "cancel_reversed")

    if subscription.status != previous.status:
        if subscription.status == "trialing":
            add("trial_started", "trial")
        elif subscription.status == "active":
            if previous.status == "trialing":
                add("trial_converted", "conversion")
            elif previous.ended_at is not None or previous.status in {"canceled", "unpaid"}:
                add("subscription_resubscribed", "resubscribed")
            else:
                add("subscription_activated", "activated")
        elif subscription.status == "past_due":
            add("subscription_past_due", "past_due")
        elif subscription.status in {"canceled", "unpaid", "incomplete_expired"}:
            if previous.status == "trialing":
                add("trial_expired", "trial_expired")

    return events
