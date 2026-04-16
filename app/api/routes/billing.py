import logging
import os
from datetime import datetime, timezone

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import StripePrice, Subscription, User, WorkspaceMember
from app.services.pricing import PLAN_SPECS, get_plan_spec, normalize_plan_key
from app.services.storage_policy import workspace_usage_payload
from app.services.stripe_catalog_sync import (
    mark_stripe_price_inactive,
    mark_stripe_product_inactive,
    resolve_checkout_price_id,
    sync_catalog_from_stripe_api,
    upsert_stripe_price_from_object,
    upsert_stripe_product_from_object,
)
from app.utils.email import send_subscription_canceled_email, send_subscription_welcome_email
from app.utils.security import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["Billing"])

stripe.api_key = os.getenv("STRIPE_SECRET_KEY") or ""

TRIAL_DAYS = 14


def _frontend_base() -> str:
    base = os.getenv("FRONTEND_BASE_URL", "http://localhost:3000").rstrip("/")
    return base


def _legacy_env_price_id(plan: str, interval: str) -> str:
    """interval: month | year (maps to MONTHLY / ANNUAL env suffixes). Legacy env fallback only."""
    canonical = normalize_plan_key(plan)
    if canonical not in ("pro", "scale"):
        raise HTTPException(status_code=400, detail="Invalid plan")
    if interval not in ("month", "year"):
        raise HTTPException(status_code=400, detail="Invalid interval; use month or year")
    suffix = "MONTHLY" if interval == "month" else "ANNUAL"
    preferred_key = f"STRIPE_PRICE_{canonical.upper()}_{suffix}"
    legacy_source = "BASIC" if canonical == "free" else "ELITE" if canonical == "scale" else canonical.upper()
    legacy_key = f"STRIPE_PRICE_{legacy_source}_{suffix}"
    price_id = os.getenv(preferred_key) or os.getenv(legacy_key)
    if not price_id:
        raise HTTPException(
            status_code=500,
            detail=f"Missing Stripe price configuration: {preferred_key}",
        )
    return price_id


def _resolve_price_id_for_checkout(db: Session, plan: str, interval: str) -> str:
    """Resolve Stripe Price id from DB catalog; optional env fallback when STRIPE_PRICE_FALLBACK is set."""
    canonical = normalize_plan_key(plan)
    if canonical not in ("pro", "scale"):
        raise HTTPException(status_code=400, detail="Invalid plan")
    if interval not in ("month", "year"):
        raise HTTPException(status_code=400, detail="Invalid interval; use month or year")

    rid = resolve_checkout_price_id(db, plan=canonical, interval=interval)
    if rid:
        return rid

    if os.getenv("STRIPE_PRICE_FALLBACK", "").lower() in ("1", "true", "yes"):
        return _legacy_env_price_id(canonical, interval)

    raise HTTPException(
        status_code=503,
        detail=(
            "Stripe catalog is not synced yet. Configure Price metadata (editube_plan, editube_interval) "
            "or lookup_key, send product/price webhooks, or run POST /billing/sync-catalog with "
            "STRIPE_CATALOG_SYNC_SECRET. Temporary fallback: set STRIPE_PRICE_FALLBACK=1 and legacy STRIPE_PRICE_* env vars."
        ),
    )


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

    canonical_plan = normalize_plan_key(body.plan)
    if canonical_plan not in ("pro", "scale"):
        raise HTTPException(status_code=400, detail="Only Pro or Scale can be purchased online")
    price_id = _resolve_price_id_for_checkout(db, canonical_plan, body.interval)

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
            "plan": canonical_plan,
        },
        subscription_data={
            "metadata": {
                "user_id": str(current_user.id),
                "plan": canonical_plan,
            },
            "trial_period_days": TRIAL_DAYS,
        },
    )
    return {"url": session.url}


@router.get("/catalog")
def get_billing_catalog(db: Session = Depends(get_db)):
    rows = (
        db.query(StripePrice)
        .filter(
            StripePrice.active.is_(True),
            StripePrice.editube_plan.isnot(None),
            StripePrice.editube_interval.isnot(None),
        )
        .all()
    )
    by_plan_interval: dict[tuple[str, str], StripePrice] = {}
    for r in rows:
        if r.editube_plan and r.editube_interval:
            by_plan_interval[(r.editube_plan, r.editube_interval)] = r

    plans_out = []
    for key, spec in PLAN_SPECS.items():
        entry = {
            "key": key,
            "label": spec.label,
            "seat_cap": spec.seat_cap,
            "included_storage_bytes": spec.included_storage_bytes,
            "storage_addon_tb_price_usd": spec.storage_addon_tb_price_usd,
            "grace_days": spec.grace_days,
            "stripe_prices": {},
        }
        for interval in ("month", "year"):
            row = by_plan_interval.get((key, interval))
            if row:
                entry["stripe_prices"][interval] = {
                    "stripe_price_id": row.stripe_price_id,
                    "unit_amount": row.unit_amount,
                    "currency": row.currency,
                }
        plans_out.append(entry)

    currency = "USD"
    for r in rows:
        if r.currency:
            currency = (r.currency or "").upper()
            break

    return {"currency": currency, "plans": plans_out}


@router.post("/sync-catalog")
def sync_billing_catalog(
    db: Session = Depends(get_db),
    x_stripe_catalog_sync_secret: str | None = Header(default=None, alias="X-Stripe-Catalog-Sync-Secret"),
):
    """Bootstrap or repair local Stripe catalog from the Stripe API (requires env STRIPE_CATALOG_SYNC_SECRET)."""
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe is not configured")
    expected = os.getenv("STRIPE_CATALOG_SYNC_SECRET")
    if not expected or (x_stripe_catalog_sync_secret or "") != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing catalog sync secret")

    n = sync_catalog_from_stripe_api(db)
    return {"synced_prices": n}


@router.get("/usage")
def get_billing_usage(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    member = (
        db.query(WorkspaceMember)
        .filter(WorkspaceMember.user_id == current_user.id)
        .order_by(WorkspaceMember.id.asc())
        .first()
    )
    if not member:
        raise HTTPException(status_code=404, detail="Workspace not found for current user")
    payload = workspace_usage_payload(db, user=current_user, workspace_id=member.workspace_id)
    plan = get_plan_spec(current_user.plan)
    payload["plan_label"] = plan.label
    return payload


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


def _plan_from_catalog_price_id(db: Session, sub: stripe.Subscription) -> str | None:
    price_id = _price_id_from_subscription(sub)
    if not price_id:
        return None
    row = db.query(StripePrice).filter(StripePrice.stripe_price_id == price_id).first()
    if not row or not row.editube_plan:
        return None
    normalized = normalize_plan_key(row.editube_plan)
    return normalized if normalized in ("free", "pro", "scale", "enterprise") else None


def _subscription_metadata_plan(sub: stripe.Subscription) -> str | None:
    meta = getattr(sub, "metadata", None)
    if not meta:
        return None
    try:
        p = meta.get("plan") if hasattr(meta, "get") else meta["plan"]
    except (KeyError, TypeError, AttributeError):
        return None
    normalized = normalize_plan_key(p if isinstance(p, str) else None)
    return normalized if normalized in ("free", "pro", "scale", "enterprise") else None


def _upsert_subscription_row(
    db: Session,
    user: User,
    sub: stripe.Subscription,
    plan_hint: str | None,
) -> None:
    normalized_hint = normalize_plan_key(plan_hint)
    plan = normalized_hint if normalized_hint in ("free", "pro", "scale", "enterprise") else None
    plan = plan or _subscription_metadata_plan(sub)
    plan = plan or _plan_from_catalog_price_id(db, sub)

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
    plan = normalize_plan_key(plan_hint or meta.get("plan"))
    if plan not in ("pro", "scale"):
        from_catalog = _plan_from_catalog_price_id(db, subscription)
        if from_catalog in ("pro", "scale"):
            plan = from_catalog
    if plan in ("pro", "scale"):
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

    if etype in ("product.created", "product.updated"):
        try:
            upsert_stripe_product_from_object(db, data)
            db.commit()
        except Exception:
            logger.exception("Stripe catalog product upsert failed for event=%s", etype)
            db.rollback()
        return {"received": True}

    if etype == "product.deleted":
        pid = data.get("id")
        if pid:
            try:
                mark_stripe_product_inactive(db, str(pid))
                db.commit()
            except Exception:
                logger.exception("Stripe catalog product delete failed")
                db.rollback()
        return {"received": True}

    if etype in ("price.created", "price.updated"):
        try:
            upsert_stripe_price_from_object(db, data)
            db.commit()
        except Exception:
            logger.exception("Stripe catalog price upsert failed for event=%s", etype)
            db.rollback()
        return {"received": True}

    if etype == "price.deleted":
        pid = data.get("id")
        if pid:
            try:
                mark_stripe_price_inactive(db, str(pid))
                db.commit()
            except Exception:
                logger.exception("Stripe catalog price delete failed")
                db.rollback()
        return {"received": True}

    return {"received": True}
