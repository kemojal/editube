import logging
import os
from datetime import datetime, timezone

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import Subscription, User
from app.utils.email import send_subscription_canceled_email, send_subscription_welcome_email
from app.utils.security import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["Billing"])

stripe.api_key = os.getenv("STRIPE_SECRET_KEY") or ""

TRIAL_DAYS = 14


def _frontend_base() -> str:
    base = os.getenv("FRONTEND_BASE_URL", "http://localhost:3000").rstrip("/")
    return base


def _price_id(plan: str, interval: str) -> str:
    """interval: month | year (maps to MONTHLY / ANNUAL env suffixes)."""
    if plan not in ("basic", "pro", "elite"):
        raise HTTPException(status_code=400, detail="Invalid plan")
    if interval not in ("month", "year"):
        raise HTTPException(status_code=400, detail="Invalid interval; use month or year")
    suffix = "MONTHLY" if interval == "month" else "ANNUAL"
    key = f"STRIPE_PRICE_{plan.upper()}_{suffix}"
    price_id = os.getenv(key)
    if not price_id:
        raise HTTPException(
            status_code=500,
            detail=f"Missing Stripe price configuration: {key}",
        )
    return price_id


class CheckoutBody(BaseModel):
    plan: str
    interval: str  # "month" | "year"


@router.post("/checkout")
def create_checkout_session(
    body: CheckoutBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe is not configured")

    price_id = _price_id(body.plan, body.interval)

    if not current_user.stripe_customer_id:
        customer = stripe.Customer.create(
            email=current_user.email,
            metadata={"user_id": str(current_user.id)},
        )
        current_user.stripe_customer_id = customer.id
        db.commit()
        db.refresh(current_user)

    base = _frontend_base()
    success_url = f"{base}/onboarding/checkout-return?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{base}/onboarding?canceled=1&step=3"

    session = stripe.checkout.Session.create(
        customer=current_user.stripe_customer_id,
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=str(current_user.id),
        metadata={
            "user_id": str(current_user.id),
            "plan": body.plan,
        },
        subscription_data={
            "metadata": {
                "user_id": str(current_user.id),
                "plan": body.plan,
            },
            "trial_period_days": TRIAL_DAYS,
        },
    )
    return {"url": session.url}


@router.post("/portal")
def create_portal_session(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe is not configured")
    if not current_user.stripe_customer_id:
        raise HTTPException(status_code=400, detail="No billing account yet")

    base = _frontend_base()
    session = stripe.billing_portal.Session.create(
        customer=current_user.stripe_customer_id,
        return_url=f"{base}/billing",
    )
    return {"url": session.url}


def _stripe_dt(ts: int | float | None):
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)


def _price_id_from_subscription(sub: stripe.Subscription) -> str | None:
    try:
        items = getattr(sub, "items", None)
        data = getattr(items, "data", None) if items else None
        if not data:
            return None
        first = data[0]
        price = getattr(first, "price", None)
        if price is None and isinstance(first, dict):
            price = first.get("price")
        if price is None:
            return None
        return getattr(price, "id", None) or (price.get("id") if isinstance(price, dict) else None)
    except (AttributeError, KeyError, TypeError, IndexError):
        return None


def _subscription_metadata_plan(sub: stripe.Subscription) -> str | None:
    meta = getattr(sub, "metadata", None)
    if not meta:
        return None
    try:
        p = meta.get("plan") if hasattr(meta, "get") else meta["plan"]
    except (KeyError, TypeError, AttributeError):
        return None
    return p if p in ("basic", "pro", "elite") else None


def _upsert_subscription_row(
    db: Session,
    user: User,
    sub: stripe.Subscription,
    plan_hint: str | None,
) -> None:
    plan = plan_hint if plan_hint in ("basic", "pro", "elite") else None
    plan = plan or _subscription_metadata_plan(sub)

    cust = sub.customer
    if cust is not None and not isinstance(cust, str):
        cust = getattr(cust, "id", None) or str(cust)

    row = (
        db.query(Subscription)
        .filter(Subscription.stripe_subscription_id == sub.id)
        .first()
    )
    if not row:
        row = Subscription(user_id=user.id, stripe_subscription_id=sub.id)
        db.add(row)

    row.user_id = user.id
    row.stripe_customer_id = cust
    row.customer_email = user.email
    row.stripe_price_id = _price_id_from_subscription(sub)
    row.status = sub.status
    if plan:
        row.plan = plan
    row.trial_start = _stripe_dt(sub.trial_start)
    row.current_period_start = _stripe_dt(sub.current_period_start)
    row.current_period_end = _stripe_dt(sub.current_period_end)
    row.cancel_at_period_end = bool(getattr(sub, "cancel_at_period_end", False))
    if sub.status in ("canceled", "unpaid", "incomplete_expired"):
        row.ended_at = row.ended_at or datetime.now(timezone.utc).replace(tzinfo=None)
    elif sub.status in ("trialing", "active"):
        row.ended_at = None


def _sync_user_from_subscription(
    db: Session,
    user: User,
    subscription: stripe.Subscription,
    plan_hint: str | None,
) -> None:
    meta = subscription.metadata or {}
    plan = plan_hint or meta.get("plan")
    if plan and plan in ("basic", "pro", "elite"):
        user.plan = plan

    user.stripe_subscription_id = subscription.id
    user.subscription_status = subscription.status
    if subscription.customer:
        cust = subscription.customer
        user.stripe_customer_id = (
            cust if isinstance(cust, str) else getattr(cust, "id", None) or str(cust)
        )

    ts = subscription.trial_start
    if ts:
        user.trial_start_date = _stripe_dt(ts)

    if subscription.status in ("trialing", "active"):
        user.onboarding_completed = True

    _upsert_subscription_row(db, user, subscription, plan_hint)

    db.commit()
    db.refresh(user)


@router.post("/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe is not configured")

    wh_secret = os.getenv("STRIPE_WEBHOOK_SECRET")
    if not wh_secret:
        raise HTTPException(status_code=500, detail="Webhook secret not configured")

    payload = await request.body()
    sig = request.headers.get("stripe-signature")
    if not sig:
        raise HTTPException(status_code=400, detail="Missing stripe-signature header")

    try:
        event = stripe.Webhook.construct_event(payload, sig, wh_secret)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    etype = event["type"]
    data = event["data"]["object"]

    if etype == "checkout.session.completed":
        session = data
        if session.get("mode") != "subscription":
            return {"received": True}

        uid = session.get("client_reference_id") or (session.get("metadata") or {}).get("user_id")
        if not uid:
            return {"received": True}

        user = db.query(User).filter(User.id == int(uid)).first()
        if not user:
            return {"received": True}

        sub_id = session.get("subscription")
        if not sub_id:
            return {"received": True}

        subscription = stripe.Subscription.retrieve(
            sub_id,
            expand=["items.data.price"],
        )
        plan = (session.get("metadata") or {}).get("plan")
        _sync_user_from_subscription(db, user, subscription, plan)
        try:
            send_subscription_welcome_email(user, subscription)
        except Exception:
            logger.exception("Welcome email failed for user_id=%s", user.id)
        return {"received": True}

    if etype == "customer.subscription.updated":
        sub = data
        uid = (sub.get("metadata") or {}).get("user_id")
        user = None
        if uid:
            user = db.query(User).filter(User.id == int(uid)).first()
        if not user and sub.get("id"):
            user = db.query(User).filter(User.stripe_subscription_id == sub["id"]).first()
        if user:
            subscription = stripe.Subscription.retrieve(
                sub["id"],
                expand=["items.data.price"],
            )
            plan = (subscription.metadata or {}).get("plan") or user.plan
            _sync_user_from_subscription(db, user, subscription, plan)
        return {"received": True}

    if etype == "customer.subscription.deleted":
        sub = data
        sid = sub["id"]
        row = (
            db.query(Subscription)
            .filter(Subscription.stripe_subscription_id == sid)
            .first()
        )
        if row:
            row.status = "canceled"
            row.ended_at = datetime.now(timezone.utc).replace(tzinfo=None)
        user = db.query(User).filter(User.stripe_subscription_id == sid).first()
        cancel_to: str | None = None
        cancel_name = "there"
        cancel_plan: str | None = None
        if user:
            cancel_to = user.email
            cancel_name = user.full_name or user.name or "there"
            cancel_plan = user.plan
            user.subscription_status = "canceled"
            user.stripe_subscription_id = None
        db.commit()
        if cancel_to:
            try:
                send_subscription_canceled_email(cancel_to, cancel_name, cancel_plan)
            except Exception:
                logger.exception("Cancellation email failed for sid=%s", sid)
        return {"received": True}

    return {"received": True}
