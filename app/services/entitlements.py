"""What a Stripe subscription actually entitles a user to.

Before this module the answer was spread across `billing.py` in three places
that disagreed with each other, and the disagreements were all in the
customer's favour:

* `_sync_user_from_subscription` wrote `user.plan` for *any* subscription
  status, so a checkout that never got paid (`incomplete`) or one that had
  already lapsed (`unpaid`, `canceled`) still granted Pro.
* `customer.subscription.deleted` cleared `subscription_status` but left
  `user.plan` alone, so cancelling bought a permanent Pro-tier storage cap.
* the plan was read from subscription *metadata*, which is stamped once at
  checkout and never updated, so plan changes made in the Stripe billing
  portal were silently ignored — the customer paid Scale and got Pro.

Nothing outside billing reads `subscription_status`; `user.plan` is the only
field the rest of the app gates on (`storage_policy`, `ugc_credits`,
seat caps). So the rule this module enforces is simply: **`user.plan` is paid
only while a subscription is in a status that means the customer is paying,
and it always reflects the price they are actually on.**
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.db.models import StripePrice, StripeProduct, Subscription, User
from app.services.pricing import PAID_PLANS, is_editube_product, resolve_plan_key

#: Statuses where the customer is on the hook for money and keeps their tier.
#: ``past_due`` is deliberately included: Stripe is still retrying the charge
#: (dunning), and yanking someone's storage mid-retry over a card that expired
#: is how you turn a payment blip into a churn event. ``unpaid`` is where
#: Stripe gives up, and that is where access goes.
ENTITLED_STATUSES: frozenset[str] = frozenset({"trialing", "active", "past_due"})

#: Statuses that mean this subscription is over and will not come back. A new
#: subscription id is required to regain access.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"canceled", "unpaid", "incomplete_expired"}
)

#: Reached checkout but never completed payment. Not entitled, but not dead
#: either — Stripe may still complete it, so it does not clear an existing
#: subscription off the user.
PENDING_STATUSES: frozenset[str] = frozenset({"incomplete", "paused"})

#: The plan a user falls back to when nothing entitles them to more.
DEFAULT_PLAN = "free"


def is_entitled(status: str | None) -> bool:
    return (status or "") in ENTITLED_STATUSES


def is_terminal(status: str | None) -> bool:
    return (status or "") in TERMINAL_STATUSES


def stripe_field(obj: Any, key: str) -> Any:
    """Read `key` off a Stripe object, a webhook dict, or None."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def stripe_datetime(ts: int | float | None) -> datetime | None:
    """Stripe unix seconds -> naive UTC datetime (the DB columns are naive)."""
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def _subscription_items(sub: Any) -> list[Any]:
    items = stripe_field(sub, "items")
    data = stripe_field(items, "data")
    if not data:
        return []
    try:
        return list(data)
    except TypeError:
        return []


def price_id_from_subscription(sub: Any) -> str | None:
    """The Price the subscription is currently billed on.

    This — not the metadata stamped at checkout — is what the customer is
    paying for right now. A plan change made in the billing portal swaps the
    price and leaves the metadata untouched.
    """
    for item in _subscription_items(sub):
        price = stripe_field(item, "price")
        price_id = stripe_field(price, "id")
        if price_id:
            return str(price_id)
    return None


def subscription_period(sub: Any) -> tuple[datetime | None, datetime | None]:
    """(current_period_start, current_period_end) as naive UTC datetimes.

    Stripe moved these fields off the Subscription object and onto its items
    in API version ``2025-03-31.basil``. The pinned SDK (stripe>=15) defaults
    to a later version still, so reading them off the subscription — which is
    what this code used to do — silently returned ``None`` for every customer:
    no renewal date in the billing panel, no "access ends" date in the
    cancellation email. Items come first, with the pre-basil location kept as
    a fallback so older pinned API versions still work.
    """
    for item in _subscription_items(sub):
        start = stripe_datetime(stripe_field(item, "current_period_start"))
        end = stripe_datetime(stripe_field(item, "current_period_end"))
        if start or end:
            return start, end
    return (
        stripe_datetime(stripe_field(sub, "current_period_start")),
        stripe_datetime(stripe_field(sub, "current_period_end")),
    )


def plan_from_price_id(db: Session, price_id: str | None) -> str | None:
    if not price_id:
        return None
    row = (
        db.query(StripePrice)
        .filter(StripePrice.stripe_price_id == price_id)
        .first()
    )
    return resolve_plan_key(row.editube_plan) if row else None


def plan_from_metadata(sub: Any) -> str | None:
    meta = stripe_field(sub, "metadata")
    value = stripe_field(meta, "plan")
    return resolve_plan_key(value if isinstance(value, str) else None)


#: A subscription this Stripe account holds that is demonstrably ours.
OURS = "ours"
#: A subscription on a price we mirror but deliberately do not sell — another
#: product sharing the Stripe account, or a retired SKU.
FOREIGN = "foreign"
#: A price we have never seen. Usually means the catalog is not synced.
UNKNOWN = "unknown"


def subscription_ownership(db: Session, sub: Any) -> str:
    """Whether this subscription is Editube's, someone else's, or unrecognised.

    One Stripe account can serve several unrelated products, and a person who
    buys two of them shares a single `cus_` id across both. Without this
    distinction a subscription to a *different product* reached the Editube
    entitlement path through the customer-id lookup and granted a tier here —
    which is exactly what a live account was doing, showing Editube `pro` on
    the strength of a subscription to another product entirely.

    The catalog mirrors every price on the account, so "we have a row for this
    price but it carries no `editube_plan`" is a positive signal that the price
    is not ours. That is different from never having seen the price at all,
    which just means the catalog is stale and nothing should be concluded.
    """
    price_id = price_id_from_subscription(sub)
    if not price_id:
        return UNKNOWN
    row = db.query(StripePrice).filter(StripePrice.stripe_price_id == price_id).first()
    if row is None:
        return UNKNOWN

    # The product name is the primary rule: only products called "Editube"
    # are ours. Checking it here means a stray `editube_plan` key on another
    # product's price cannot buy its way into an entitlement.
    product = (
        db.query(StripeProduct)
        .filter(StripeProduct.stripe_product_id == row.stripe_product_id)
        .first()
    )
    if is_editube_product(product.name if product else None) is False:
        return FOREIGN

    # An Editube price with no plan mapping is a retired or unsold SKU
    # ("Editube Basic"), which grants nothing — same outcome as foreign.
    return OURS if resolve_plan_key(row.editube_plan) else FOREIGN


def resolve_subscription_plan(
    db: Session, sub: Any, *, hint: str | None = None
) -> str | None:
    """The plan this subscription grants, most authoritative source first.

    The live price wins. Metadata is a fallback for subscriptions created
    before the catalog was synced, and the caller's hint is the last resort —
    the reverse of the original precedence, which let a stale checkout-time
    metadata value override the price the customer had since switched to.

    A price we mirror but do not sell grants nothing, and crucially does *not*
    fall through to metadata: another product's subscription must not be able
    to claim an Editube tier just by carrying a `plan` key.
    """
    ownership = subscription_ownership(db, sub)
    if ownership == FOREIGN:
        return None
    from_price = plan_from_price_id(db, price_id_from_subscription(sub))
    if from_price:
        return from_price
    return plan_from_metadata(sub) or resolve_plan_key(hint)


def _has_other_entitled_subscription(
    db: Session, user_id: int, *, excluding: str | None
) -> Subscription | None:
    """Another live subscription for this user, if any.

    Cancelling one of two subscriptions must not drop the user to Free while
    the other is still being paid for.
    """
    query = db.query(Subscription).filter(
        Subscription.user_id == user_id,
        Subscription.status.in_(sorted(ENTITLED_STATUSES)),
    )
    if excluding:
        query = query.filter(Subscription.stripe_subscription_id != excluding)
    return query.order_by(Subscription.id.desc()).first()


def effective_plan_for_user(
    db: Session, user: User, *, ignoring: str | None = None
) -> str:
    """The plan the user's subscription rows actually support.

    `ignoring` skips one subscription id — used when a cancellation is being
    processed and its row has not been written yet.
    """
    row = _has_other_entitled_subscription(db, user.id, excluding=ignoring)
    if row and resolve_plan_key(row.plan) in PAID_PLANS:
        return resolve_plan_key(row.plan) or DEFAULT_PLAN
    return DEFAULT_PLAN


def apply_plan(db: Session, user: User, plan: str | None) -> bool:
    """Move the user to `plan`, resetting anything scoped to the old tier.

    Returns True when the tier actually changed.
    """
    target = resolve_plan_key(plan) or DEFAULT_PLAN
    current = resolve_plan_key(user.plan) or DEFAULT_PLAN
    if target == current:
        return False

    user.plan = target
    # The storage grace window is a one-off allowance against a specific cap.
    # Carrying it across a tier change is wrong in both directions: a user who
    # burned their grace on Free would arrive at Pro with none left, and a user
    # dropping to Free would keep a window they are no longer owed.
    user.storage_grace_until = None
    return True


def sync_user_entitlement(
    db: Session,
    user: User,
    *,
    status: str | None,
    plan: str | None,
    subscription_id: str | None,
) -> None:
    """Point the user at `subscription_id` and set the tier it entitles them to.

    Called for every subscription state change. When the subscription no
    longer entitles anything, the user falls back to whatever *other* live
    subscription they have, and to Free if there is none.
    """
    user.subscription_status = status

    if is_entitled(status):
        user.stripe_subscription_id = subscription_id
        if resolve_plan_key(plan) in PAID_PLANS:
            apply_plan(db, user, plan)
        # An entitled subscription whose plan cannot be resolved — catalog not
        # synced, no metadata, no hint — is a configuration problem, not a
        # reason to revoke a paying customer's tier. Leave it where it is and
        # let the next sync (or `POST /billing/sync-catalog`) settle it.
        return

    if is_terminal(status):
        # Only detach if this is the subscription the user is currently
        # pointed at. A terminal event for an older subscription must not
        # unhook the one they resubscribed with.
        if user.stripe_subscription_id == subscription_id:
            fallback = _has_other_entitled_subscription(
                db, user.id, excluding=subscription_id
            )
            user.stripe_subscription_id = (
                fallback.stripe_subscription_id if fallback else None
            )
        apply_plan(db, user, effective_plan_for_user(db, user, ignoring=subscription_id))
        return

    # incomplete / paused / unknown: no entitlement granted, but the pointer is
    # left alone so a subscription that later completes is not orphaned.
    apply_plan(db, user, effective_plan_for_user(db, user, ignoring=subscription_id))


def has_used_trial(db: Session, user: User) -> bool:
    """Whether this account has already consumed a free trial.

    Without this check the trial renews itself: subscribe, cancel inside the
    14 days, subscribe again — forever, for free, for the cost of two clicks a
    fortnight.

    Any subscription row that ever carried a `trial_start` counts.
    `user.trial_start_date` also counts, but only for an account that has been
    through Stripe at all: the free-onboarding path used to stamp that column
    as a signup date, and treating those as spent trials would deny a first
    trial to every existing free user.
    """
    if user.trial_start_date is not None and user.stripe_customer_id:
        return True
    return (
        db.query(Subscription.id)
        .filter(
            Subscription.user_id == user.id,
            Subscription.trial_start.isnot(None),
        )
        .first()
        is not None
    )


def live_subscriptions(db: Session, user_id: int) -> Iterable[Subscription]:
    """Subscriptions that would double-bill if another checkout completed."""
    return (
        db.query(Subscription)
        .filter(
            Subscription.user_id == user_id,
            Subscription.status.in_(sorted(ENTITLED_STATUSES | {"incomplete"})),
        )
        .order_by(Subscription.id.desc())
        .all()
    )
