import logging
import os
from datetime import datetime, timezone
from urllib.parse import quote

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import StripePrice, StripeWebhookEvent, Subscription, User, WorkspaceMember
from app.services.entitlements import (
    ENTITLED_STATUSES,
    FOREIGN,
    PENDING_STATUSES,
    has_used_trial,
    is_entitled,
    is_terminal,
    live_subscriptions,
    price_id_from_subscription,
    resolve_subscription_plan,
    stripe_datetime,
    subscription_ownership,
    stripe_field,
    subscription_period,
    sync_user_entitlement,
)
from app.services.pricing import (
    PAID_PLANS,
    PLAN_SPECS,
    SELF_SERVE_PLANS,
    get_plan_spec,
    normalize_plan_key,
    resolve_plan_key,
)
from app.services.marketing_offers import active_marketing_offer, resolve_checkout_offer
from app.services.referrals import (
    mark_pass_redeemed,
    sync_referral_from_subscription,
    trial_days_for_user,
)
from app.services.storage_policy import workspace_usage_payload
from app.services.workspace_bootstrap import ensure_personal_workspace
from app.services.stripe_catalog_sync import (
    mark_stripe_price_inactive,
    stripe_object_to_dict,
    mark_stripe_product_inactive,
    resolve_checkout_price_id,
    sync_catalog_from_stripe_api,
    upsert_stripe_price_from_object,
    upsert_stripe_product_from_object,
)
from app.utils.email import (
    send_payment_failed_email,
    send_subscription_canceled_email,
    send_subscription_welcome_email,
    send_subscription_will_not_renew_email,
    send_trial_ending_email,
)
from app.utils.security import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["Billing"])

stripe.api_key = os.getenv("STRIPE_SECRET_KEY") or ""

TRIAL_DAYS = 14


def _frontend_base() -> str:
    base = os.getenv("FRONTEND_BASE_URL", "http://localhost:3000").rstrip("/")
    return base


def _legacy_env_price_id(plan: str, interval: str) -> str | None:
    """Legacy `STRIPE_PRICE_<PLAN>_<MONTHLY|ANNUAL>` fallback, or None if unset.

    Returns None rather than raising: a plan with no configured price is "not
    for sale", which is a 409 the caller composes, not a 500. Raising here
    turned an unsold plan into an *error* — asking for Scale, which has no
    Stripe product at all, produced `500 Missing Stripe price configuration`.
    """
    canonical = normalize_plan_key(plan)
    if canonical not in SELF_SERVE_PLANS or interval not in ("month", "year"):
        return None
    suffix = "MONTHLY" if interval == "month" else "ANNUAL"
    legacy_source = "ELITE" if canonical == "scale" else canonical.upper()
    return os.getenv(f"STRIPE_PRICE_{canonical.upper()}_{suffix}") or os.getenv(
        f"STRIPE_PRICE_{legacy_source}_{suffix}"
    )


def purchasable_price_id(db: Session, plan: str, interval: str) -> str | None:
    """The Stripe Price a plan/interval can actually be bought at, if any."""
    canonical = normalize_plan_key(plan)
    if canonical not in SELF_SERVE_PLANS or interval not in ("month", "year"):
        return None
    rid = resolve_checkout_price_id(db, plan=canonical, interval=interval)
    if rid:
        return rid
    if os.getenv("STRIPE_PRICE_FALLBACK", "").lower() in ("1", "true", "yes"):
        return _legacy_env_price_id(canonical, interval)
    return None


def _resolve_price_id_for_checkout(db: Session, plan: str, interval: str) -> str:
    canonical = normalize_plan_key(plan)
    if canonical not in SELF_SERVE_PLANS:
        raise HTTPException(status_code=400, detail="Invalid plan")
    if interval not in ("month", "year"):
        raise HTTPException(status_code=400, detail="Invalid interval; use month or year")

    rid = purchasable_price_id(db, canonical, interval)
    if rid:
        return rid

    raise HTTPException(
        status_code=409,
        detail=(
            f"The {get_plan_spec(canonical).label} plan is not available for purchase yet. "
            "Please contact support if you were expecting to subscribe to it."
        ),
    )


#: Re-exported so existing imports keep working; the implementation moved to
#: `entitlements` because the webhook, the checkout return, and the plan
#: resolver all need it.
_stripe_field = stripe_field


class CheckoutBody(BaseModel):
    plan: str
    interval: str  # "month" | "year"
    campaign: str | None = None
    #: Where to send the customer after Stripe. Path-only, validated below —
    #: the old hardcoded onboarding URLs dumped anyone upgrading from account
    #: settings back into the signup wizard.
    return_path: str | None = None


def _safe_return_path(raw: str | None, default: str) -> str:
    """A same-origin path, or `default`.

    Checkout success/cancel URLs are attacker-influenceable input that Stripe
    will redirect a logged-in user to, so anything that could leave the origin
    (absolute URLs, protocol-relative `//evil`, backslash variants) is
    rejected rather than sanitised.
    """
    candidate = (raw or "").strip()
    if not candidate.startswith("/"):
        return default
    if candidate.startswith("//") or candidate.startswith("/\\"):
        return default
    if "\\" in candidate or "\n" in candidate or "\r" in candidate:
        return default
    return candidate


@router.get("/offers/active")
def get_active_marketing_offer():
    offer = active_marketing_offer()
    return {"offer": offer.public_payload() if offer else None}


@router.post("/checkout")
def create_checkout_session(
    body: CheckoutBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe is not configured")

    canonical_plan = resolve_plan_key(body.plan)
    if canonical_plan not in SELF_SERVE_PLANS:
        raise HTTPException(
            status_code=400,
            detail="That plan cannot be purchased online. Contact sales for Enterprise.",
        )

    # A customer who already has a live subscription must change it, not buy a
    # second one. Stripe will happily create the duplicate and bill for both,
    # and the local model only tracks one `stripe_subscription_id` per user, so
    # the second purchase would also orphan the first from the UI that could
    # cancel it.
    existing = live_subscriptions(db, current_user.id)
    if existing:
        raise HTTPException(
            status_code=409,
            detail=(
                "You already have a subscription. Use the billing portal to change "
                "your plan or payment method."
            ),
        )

    price_id = _resolve_price_id_for_checkout(db, canonical_plan, body.interval)
    offer = resolve_checkout_offer(
        body.campaign,
        plan=canonical_plan,
        interval=body.interval,
    )
    if body.campaign and offer is None:
        raise HTTPException(status_code=409, detail="This offer has expired or does not apply to the selected plan")

    if not current_user.stripe_customer_id:
        customer = stripe.Customer.create(
            email=current_user.email,
            metadata={"user_id": str(current_user.id)},
        )
        current_user.stripe_customer_id = customer.id
        db.commit()
        db.refresh(current_user)

    base = _frontend_base()
    return_path = _safe_return_path(body.return_path, "/onboarding")
    success_url = (
        f"{base}/onboarding/checkout-return?session_id={{CHECKOUT_SESSION_ID}}"
        f"&next={quote(return_path, safe='')}"
    )
    cancel_url = f"{base}{return_path}{'&' if '?' in return_path else '?'}canceled=1"

    metadata = {
        "user_id": str(current_user.id),
        "plan": canonical_plan,
    }
    if offer:
        metadata["campaign"] = offer.campaign_id

    # A guest pass buys a longer trial than the standing one. Both are offered
    # once per account: without that check, cancelling inside the trial window
    # and buying again renewed it, and the service was permanently free to
    # anyone willing to click twice a fortnight.
    trial_days = 0 if has_used_trial(db, current_user) else trial_days_for_user(
        db, current_user.id, TRIAL_DAYS
    )
    redeeming_pass = trial_days not in (0, TRIAL_DAYS)
    if redeeming_pass:
        metadata["referral_pass"] = "1"

    checkout_options = {
        "customer": current_user.stripe_customer_id,
        "mode": "subscription",
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": str(current_user.id),
        "metadata": metadata,
        "subscription_data": {"metadata": metadata},
    }
    # Stripe rejects `trial_period_days: 0`; omitting the key is how you say
    # "bill immediately".
    if trial_days > 0:
        checkout_options["subscription_data"]["trial_period_days"] = trial_days
    if offer:
        checkout_options["discounts"] = [{"promotion_code": offer.stripe_promotion_code_id}]
    else:
        checkout_options["allow_promotion_codes"] = True

    session = stripe.checkout.Session.create(**checkout_options)

    # Burn the referral pass only once Stripe has actually issued the session.
    # Marking it beforehand meant a Stripe outage silently consumed a pass the
    # user never got to use.
    if redeeming_pass:
        mark_pass_redeemed(db, current_user.id)
        db.commit()

    return {"url": session.url}


@router.get("/checkout-session-status")
def get_checkout_session_status(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe is not configured")

    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except stripe.error.InvalidRequestError as exc:
        raise HTTPException(status_code=404, detail="Checkout session not found") from exc

    if _stripe_field(session, "mode") != "subscription":
        raise HTTPException(status_code=400, detail="Checkout session is not a subscription session")

    metadata = _stripe_field(session, "metadata") or {}
    uid = _stripe_field(session, "client_reference_id") or _stripe_field(metadata, "user_id")
    if str(uid or "") != str(current_user.id):
        raise HTTPException(status_code=403, detail="Not authorized to inspect this checkout session")

    sub_id = _stripe_field(session, "subscription")
    if not sub_id:
        return {
            "synced": False,
            "onboarding_completed": bool(current_user.onboarding_completed),
            "subscription_status": current_user.subscription_status,
            "plan": current_user.plan,
        }

    subscription = stripe.Subscription.retrieve(
        sub_id,
        expand=["items.data.price"],
    )
    # The hint is the last-resort source only; `_sync_user_from_subscription`
    # prefers the price the subscription is actually on. Passing
    # `current_user.plan` as a fallback used to make an already-Pro user's own
    # tier win over whatever they had just bought.
    _sync_user_from_subscription(
        db, current_user, subscription, _stripe_field(metadata, "plan")
    )

    return {
        "synced": bool(current_user.onboarding_completed),
        "onboarding_completed": bool(current_user.onboarding_completed),
        "subscription_status": current_user.subscription_status,
        "plan": current_user.plan,
    }


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
        # Free needs no price to be "available"; Enterprise is sales-led and
        # deliberately has none. Everything else is only real if it can be
        # bought — Scale was advertised here with no Stripe product behind it
        # at all, so choosing it 500'd at checkout.
        if key in SELF_SERVE_PLANS:
            entry["purchasable"] = any(
                purchasable_price_id(db, key, interval) for interval in ("month", "year")
            )
        else:
            entry["purchasable"] = key == "free"
        entry["contact_sales"] = key == "enterprise"

        # A self-serve plan nobody can buy is not shown at all. Listing it with
        # no price is what produced a pricing page with a button that errored.
        if key in SELF_SERVE_PLANS and not entry["purchasable"]:
            logger.warning("Plan %s has no purchasable price; omitting from catalog", key)
            continue
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


def _subscription_detail_dict(db: Session, user: User) -> dict | None:
    """Snapshot for billing UI from local Subscription row (kept in sync via webhooks / checkout)."""
    if not user.stripe_subscription_id:
        return None
    row = (
        db.query(Subscription)
        .filter(Subscription.stripe_subscription_id == user.stripe_subscription_id)
        .first()
    )
    if not row:
        return {
            "stripe_subscription_id": user.stripe_subscription_id,
            "status": user.subscription_status,
            "plan": user.plan,
            "current_period_start": None,
            "current_period_end": None,
            "cancel_at_period_end": False,
            "trial_start": user.trial_start_date.isoformat() if user.trial_start_date else None,
        }
    return {
        "stripe_subscription_id": row.stripe_subscription_id,
        "status": row.status,
        "plan": row.plan or user.plan,
        "current_period_start": row.current_period_start.isoformat() if row.current_period_start else None,
        "current_period_end": row.current_period_end.isoformat() if row.current_period_end else None,
        "cancel_at_period_end": bool(row.cancel_at_period_end),
        "trial_start": row.trial_start.isoformat() if row.trial_start else None,
    }


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
    # Accounts created before signup started bootstrapping a personal workspace
    # have no membership row at all, and a 404 here read to the user as "your
    # subscription is gone". Create the workspace the way signup would instead
    # — `ensure_personal_workspace` is idempotent, so this is a no-op for
    # everyone else. Same self-heal `projects` and `google_drive` already do.
    workspace_id = member.workspace_id if member else ensure_personal_workspace(db, current_user).id
    payload = workspace_usage_payload(db, user=current_user, workspace_id=workspace_id)
    plan = get_plan_spec(current_user.plan)
    payload["plan_label"] = plan.label
    payload["subscription_detail"] = _subscription_detail_dict(db, current_user)
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
        return_url=f"{base}/dashboard?account=billing",
    )
    return {"url": session.url}


@router.get("/invoices")
def list_invoices(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return the customer's Stripe invoices (most recent first)."""
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe is not configured")
    # No Stripe customer yet simply means no billing history — not an error.
    if not current_user.stripe_customer_id:
        return {"invoices": []}

    try:
        resp = stripe.Invoice.list(customer=current_user.stripe_customer_id, limit=24)
    except Exception as exc:  # pragma: no cover - surfaced to client as 502
        raise HTTPException(status_code=502, detail=f"Stripe error: {exc}")

    invoices = [
        {
            "id": _stripe_field(inv, "id"),
            "number": _stripe_field(inv, "number"),
            "amount_paid": _stripe_field(inv, "amount_paid"),
            "amount_due": _stripe_field(inv, "amount_due"),
            "currency": _stripe_field(inv, "currency"),
            "status": _stripe_field(inv, "status"),
            "created": _stripe_field(inv, "created"),
            "hosted_invoice_url": _stripe_field(inv, "hosted_invoice_url"),
            "invoice_pdf": _stripe_field(inv, "invoice_pdf"),
        }
        for inv in (getattr(resp, "data", None) or [])
    ]
    return {"invoices": invoices}


#: Re-exported for callers that still import the private names.
_stripe_dt = stripe_datetime
_price_id_from_subscription = price_id_from_subscription


def _upsert_subscription_row(
    db: Session,
    user: User,
    sub: stripe.Subscription,
    plan_hint: str | None,
) -> Subscription:
    # `resolve_subscription_plan` puts the live price ahead of both metadata
    # and the hint. The old order started from `normalize_plan_key(plan_hint)`,
    # which returns "free" for a missing hint rather than None — so the
    # metadata and catalog lookups after it were dead code, and every row
    # synced without an explicit hint was stamped `plan="free"` no matter what
    # the customer had bought.
    plan = resolve_subscription_plan(db, sub, hint=plan_hint)

    sub_id = _stripe_field(sub, "id")
    status = _stripe_field(sub, "status")
    cust = _stripe_field(sub, "customer")
    if cust is not None and not isinstance(cust, str):
        cust = getattr(cust, "id", None) or str(cust)

    row = (
        db.query(Subscription)
        .filter(Subscription.stripe_subscription_id == sub_id)
        .first()
    )
    if not row:
        row = Subscription(user_id=user.id, stripe_subscription_id=sub_id)
        db.add(row)

    row.user_id = user.id
    row.stripe_customer_id = cust
    row.customer_email = user.email
    row.stripe_price_id = price_id_from_subscription(sub)
    row.status = status
    if plan:
        row.plan = plan
    row.trial_start = stripe_datetime(_stripe_field(sub, "trial_start"))
    period_start, period_end = subscription_period(sub)
    row.current_period_start = period_start
    row.current_period_end = period_end
    row.cancel_at_period_end = bool(_stripe_field(sub, "cancel_at_period_end"))
    if is_terminal(status):
        row.ended_at = row.ended_at or datetime.now(timezone.utc).replace(tzinfo=None)
    elif status in ("trialing", "active"):
        row.ended_at = None
    return row


def _sync_user_from_subscription(
    db: Session,
    user: User,
    subscription: stripe.Subscription,
    plan_hint: str | None,
) -> None:
    sub_id = _stripe_field(subscription, "id")
    status = _stripe_field(subscription, "status")
    plan = resolve_subscription_plan(db, subscription, hint=plan_hint)

    cust = _stripe_field(subscription, "customer")
    if cust:
        user.stripe_customer_id = (
            cust if isinstance(cust, str) else getattr(cust, "id", None) or str(cust)
        )

    trial_start = stripe_datetime(_stripe_field(subscription, "trial_start"))
    if trial_start:
        # Never overwritten once set: this is the record that the account has
        # consumed its one free trial, and resubscribing must not reset it.
        user.trial_start_date = user.trial_start_date or trial_start

    # The row has to exist before the entitlement is computed — the fallback
    # for a cancellation is "whatever other subscription this user still has",
    # and that query reads these rows.
    _upsert_subscription_row(db, user, subscription, plan_hint)
    db.flush()

    sync_user_entitlement(
        db, user, status=status, plan=plan, subscription_id=sub_id
    )

    # Onboarding is "finished" the moment the account is genuinely on a paid
    # tier. Statuses like `incomplete` mean the card never cleared, and used to
    # complete onboarding — and grant the plan — anyway.
    if is_entitled(status):
        user.onboarding_completed = True

    db.commit()
    db.refresh(user)

    # If this user arrived on someone's invite link, this is where that referral
    # advances and — once the subscription is paid-active — where the referrer
    # is credited. A no-op for anyone who was not referred, and it never raises.
    sync_referral_from_subscription(db, user, user.subscription_status, user.plan)


def _user_id_from(value) -> int | None:
    """Parse a Stripe-supplied user id without letting junk 500 the webhook.

    `int(uid)` on an unparseable value raised, which returned 500, which made
    Stripe retry the same poisoned event on a backoff for three days.
    """
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _user_for_subscription(db: Session, sub, sub_id: str | None) -> User | None:
    """Find the account a subscription belongs to.

    Four lookups, widest last. The customer-id lookup is what makes
    subscriptions created outside our checkout — in the Stripe dashboard, or by
    a portal flow that does not carry our metadata — reachable at all; without
    it those events were silently dropped and the customer paid for nothing.

    It is also the one that can go wrong on a Stripe account shared between
    products, because a person who buys two of them has one `cus_` id for both.
    So it is used only when the subscription is not demonstrably someone
    else's; `subscription_ownership` gates that.
    """
    uid = _user_id_from(stripe_field(stripe_field(sub, "metadata"), "user_id"))
    if uid is not None:
        user = db.query(User).filter(User.id == uid).first()
        if user:
            return user

    if sub_id:
        user = db.query(User).filter(User.stripe_subscription_id == sub_id).first()
        if user:
            return user
        row = (
            db.query(Subscription)
            .filter(Subscription.stripe_subscription_id == sub_id)
            .first()
        )
        if row:
            user = db.query(User).filter(User.id == row.user_id).first()
            if user:
                return user

    if subscription_ownership(db, sub) == FOREIGN:
        return None

    cust = stripe_field(sub, "customer")
    if cust is not None and not isinstance(cust, str):
        cust = getattr(cust, "id", None)
    if cust:
        return db.query(User).filter(User.stripe_customer_id == str(cust)).first()
    return None


def _claim_event(db: Session, event_id: str | None, event_type: str | None) -> bool:
    """Record `event_id` as processed. False when it was already handled.

    Stripe redelivers on any non-2xx and occasionally duplicates deliveries
    outright. The database writes downstream are upserts and survive that; the
    emails are not, and customers were getting a second welcome or a second
    "your plan will not renew" for the same state change.
    """
    if not event_id:
        return True
    exists = (
        db.query(StripeWebhookEvent)
        .filter(StripeWebhookEvent.stripe_event_id == event_id)
        .first()
    )
    if exists:
        return False
    db.add(StripeWebhookEvent(stripe_event_id=event_id, event_type=event_type))
    try:
        db.commit()
    except IntegrityError:
        # A concurrent delivery of the same event won the unique index.
        db.rollback()
        return False
    return True


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

    # NB: `data` is a `StripeObject`, which since stripe>=12 is **not** a dict
    # and has no `.get()`. Every accessor below therefore goes through
    # `_stripe_field` (a `getattr(..., default)`), never `.get()`. Using `.get()`
    # here raised `AttributeError: get` on the first line of every branch, so
    # every delivery 500'd and Stripe eventually disabled the endpoint.
    if not _claim_event(db, _stripe_field(event, "id"), etype):
        return {"received": True, "duplicate": True}

    if etype == "checkout.session.completed":
        session = data
        if _stripe_field(session, "mode") != "subscription":
            return {"received": True}

        meta = _stripe_field(session, "metadata")
        uid = _user_id_from(
            _stripe_field(session, "client_reference_id") or _stripe_field(meta, "user_id")
        )
        if uid is None:
            return {"received": True}

        user = db.query(User).filter(User.id == uid).first()
        if not user:
            return {"received": True}

        sub_id = _stripe_field(session, "subscription")
        if not sub_id:
            return {"received": True}

        subscription = stripe.Subscription.retrieve(
            sub_id,
            expand=["items.data.price"],
        )
        plan = _stripe_field(meta, "plan")
        _sync_user_from_subscription(db, user, subscription, plan)
        # A session that completed without the subscription reaching a paying
        # status (card declined on the first invoice) is not a welcome.
        if is_entitled(_stripe_field(subscription, "status")):
            try:
                send_subscription_welcome_email(user, subscription)
            except Exception:
                logger.exception("Welcome email failed for user_id=%s", user.id)
        return {"received": True}

    if etype in ("customer.subscription.updated", "customer.subscription.created"):
        sub = data
        sid = _stripe_field(sub, "id")
        if not sid:
            return {"received": True}

        prev_row = (
            db.query(Subscription).filter(Subscription.stripe_subscription_id == sid).first()
        )
        prev_cancel_at_end = bool(prev_row.cancel_at_period_end) if prev_row else False

        user = _user_for_subscription(db, sub, sid)
        if not user:
            logger.warning("Stripe subscription %s has no matching user", sid)
            return {"received": True}

        subscription = stripe.Subscription.retrieve(sid, expand=["items.data.price"])
        # A subscription on a price we mirror but do not sell belongs to
        # another product on this Stripe account. Touching the user record for
        # it would grant or revoke an Editube tier off someone else's purchase.
        if subscription_ownership(db, subscription) == FOREIGN:
            logger.info("Ignoring non-Editube subscription %s", sid)
            return {"received": True, "ignored": "foreign_product"}

        # Deliberately no `or user.plan` fallback. Passing the user's current
        # tier as the hint made a stale value beat the live price, so a plan
        # switched in the billing portal never took effect — a customer who
        # upgraded to Scale kept Pro's caps, and one who downgraded to Pro kept
        # Scale's. `resolve_subscription_plan` reads the price first now, and
        # the hint is only consulted when the catalog has no row for it.
        _sync_user_from_subscription(
            db, user, subscription, _stripe_field(_stripe_field(subscription, "metadata"), "plan")
        )

        after_row = (
            db.query(Subscription).filter(Subscription.stripe_subscription_id == sid).first()
        )
        if (
            after_row
            and not prev_cancel_at_end
            and after_row.cancel_at_period_end
            and after_row.status in ("active", "trialing")
            and user.email
        ):
            try:
                send_subscription_will_not_renew_email(
                    user.email,
                    user.full_name or user.name or "there",
                    after_row.plan or user.plan,
                    after_row.current_period_end,
                )
            except Exception:
                logger.exception("Will-not-renew email failed for user_id=%s", user.id)
        return {"received": True}

    if etype == "customer.subscription.deleted":
        sub = data
        sid = _stripe_field(sub, "id")
        if not sid:
            return {"received": True}

        row = (
            db.query(Subscription)
            .filter(Subscription.stripe_subscription_id == sid)
            .first()
        )
        access_until = row.current_period_end if row else None
        if access_until is None:
            access_until = subscription_period(sub)[1]
        if row:
            row.status = "canceled"
            row.ended_at = row.ended_at or datetime.now(timezone.utc).replace(tzinfo=None)

        user = _user_for_subscription(db, sub, sid)
        cancel_to: str | None = None
        cancel_name = "there"
        cancel_plan: str | None = None
        if user:
            cancel_to = user.email
            cancel_name = user.full_name or user.name or "there"
            # The tier they are losing, captured before the downgrade.
            cancel_plan = (row.plan if row else None) or user.plan
            db.flush()
            # Previously this only cleared `subscription_status` and left
            # `user.plan` on "pro"/"scale" forever — so cancelling bought a
            # permanent Pro storage cap, Pro UGC credits and Pro seats, for
            # free. `sync_user_entitlement` drops them to whatever they still
            # actually pay for, which is normally Free.
            sync_user_entitlement(
                db, user, status="canceled", plan=cancel_plan, subscription_id=sid
            )
        db.commit()
        if cancel_to:
            try:
                send_subscription_canceled_email(
                    cancel_to,
                    cancel_name,
                    cancel_plan,
                    access_until=access_until,
                    stripe_subscription_id=sid,
                )
            except Exception:
                logger.exception("Cancellation email failed for sid=%s", sid)
        return {"received": True}

    if etype == "customer.subscription.trial_will_end":
        sub = data
        sid = _stripe_field(sub, "id")
        user = _user_for_subscription(db, sub, sid) if sid else None
        if user and user.email:
            try:
                send_trial_ending_email(
                    user.email,
                    user.full_name or user.name or "there",
                    user.plan,
                    stripe_datetime(_stripe_field(sub, "trial_end")),
                )
            except Exception:
                logger.exception("Trial-ending email failed for sid=%s", sid)
        return {"received": True}

    if etype == "invoice.payment_failed":
        # Stripe also emits `customer.subscription.updated` with status
        # `past_due`, which is what actually moves the entitlement. This branch
        # exists so the customer hears about it before dunning runs out.
        invoice = data
        sub_id = _stripe_field(invoice, "subscription")
        if sub_id is not None and not isinstance(sub_id, str):
            sub_id = _stripe_field(sub_id, "id")
        user = (
            db.query(User).filter(User.stripe_subscription_id == str(sub_id)).first()
            if sub_id
            else None
        )
        if user is None:
            cust = _stripe_field(invoice, "customer")
            if cust is not None and not isinstance(cust, str):
                cust = _stripe_field(cust, "id")
            if cust:
                user = db.query(User).filter(User.stripe_customer_id == str(cust)).first()
        if user and user.email:
            try:
                send_payment_failed_email(
                    user.email,
                    user.full_name or user.name or "there",
                    user.plan,
                    _stripe_field(invoice, "hosted_invoice_url"),
                )
            except Exception:
                logger.exception("Payment-failed email failed for sub=%s", sub_id)
        return {"received": True}

    if etype in ("product.created", "product.updated"):
        try:
            # `data` is a StripeObject; the catalog upserts index with .get().
            upsert_stripe_product_from_object(db, stripe_object_to_dict(data))
            db.commit()
        except Exception:
            logger.exception("Stripe catalog product upsert failed for event=%s", etype)
            db.rollback()
        return {"received": True}

    if etype == "product.deleted":
        pid = _stripe_field(data, "id")
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
            upsert_stripe_price_from_object(db, stripe_object_to_dict(data))
            db.commit()
        except Exception:
            logger.exception("Stripe catalog price upsert failed for event=%s", etype)
            db.rollback()
        return {"received": True}

    if etype == "price.deleted":
        pid = _stripe_field(data, "id")
        if pid:
            try:
                mark_stripe_price_inactive(db, str(pid))
                db.commit()
            except Exception:
                logger.exception("Stripe catalog price delete failed")
                db.rollback()
        return {"received": True}

    return {"received": True}
