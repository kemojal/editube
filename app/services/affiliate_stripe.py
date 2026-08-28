"""Stripe object hydration and account lookup shared by affiliate ingestion.

Webhook ingestion and manual reconciliation must resolve the same customer and
the same complete invoice-line set. Keeping the logic here prevents the repair
path from quietly using different accounting inputs than the live webhook.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import Subscription, User
from app.services.stripe_catalog_sync import stripe_object_to_dict


def stripe_field(obj, key: str):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def invoice_subscription_id(invoice) -> str | None:
    subscription = stripe_field(invoice, "subscription")
    if subscription is not None and not isinstance(subscription, str):
        subscription = stripe_field(subscription, "id")
    if subscription:
        return str(subscription)
    parent = stripe_field(invoice, "parent")
    details = stripe_field(parent, "subscription_details")
    subscription = stripe_field(details, "subscription")
    if subscription is not None and not isinstance(subscription, str):
        subscription = stripe_field(subscription, "id")
    return str(subscription) if subscription else None


def user_for_invoice(db: Session, invoice) -> User | None:
    subscription_id = invoice_subscription_id(invoice)
    if subscription_id:
        user = (
            db.query(User)
            .filter(User.stripe_subscription_id == subscription_id)
            .first()
        )
        if user:
            return user
        subscription = (
            db.query(Subscription)
            .filter(Subscription.stripe_subscription_id == subscription_id)
            .first()
        )
        if subscription:
            return db.query(User).filter(User.id == subscription.user_id).first()

    customer = stripe_field(invoice, "customer")
    if customer is not None and not isinstance(customer, str):
        customer = stripe_field(customer, "id")
    return (
        db.query(User).filter(User.stripe_customer_id == str(customer)).first()
        if customer
        else None
    )


def invoice_with_complete_lines(invoice, stripe_module):
    """Return an invoice whose `lines.data` contains every Stripe line.

    Incomplete input never falls back to an estimate. A Stripe failure bubbles
    to the caller so webhook ingestion can release its event claim and retry.
    """
    lines = stripe_field(invoice, "lines")
    if not bool(stripe_field(lines, "has_more")):
        return invoice
    invoice_id = str(stripe_field(invoice, "id") or "").strip()
    if not invoice_id:
        raise RuntimeError("Cannot hydrate invoice lines without an invoice id.")

    page = stripe_module.Invoice.list_lines(invoice_id, limit=100)
    auto_paging_iter = getattr(page, "auto_paging_iter", None)
    if callable(auto_paging_iter):
        complete_lines = list(auto_paging_iter())
    else:
        if bool(stripe_field(page, "has_more")):
            raise RuntimeError("Stripe returned an incomplete invoice line page.")
        complete_lines = list(stripe_field(page, "data") or [])

    hydrated = stripe_object_to_dict(invoice)
    hydrated["lines"] = {
        "data": [stripe_object_to_dict(line) for line in complete_lines],
        "has_more": False,
    }
    return hydrated
