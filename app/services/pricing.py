from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class PlanSpec:
    key: str
    label: str
    seat_cap: int | None
    included_storage_bytes: int
    storage_addon_tb_price_usd: int | None
    grace_days: int
    ugc_credits_monthly: int = 0  # AI UGC render credits granted per calendar month


TB_IN_BYTES: Final[int] = 1024 * 1024 * 1024 * 1024
GB_IN_BYTES: Final[int] = 1024 * 1024 * 1024

PLAN_ALIASES: Final[dict[str, str]] = {
    # New packaging names
    "free": "free",
    # `basic` was an alias for `free` while no Basic product was sold. It is a
    # real paid tier now, so it maps to itself — anything that used to write
    # "basic" meaning "free" would otherwise silently grant a paid tier. No
    # rows carried the value at the time of the change, so nothing migrated.
    "basic": "basic",
    "pro": "pro",
    "scale": "scale",
    "enterprise": "enterprise",
    # Backward compatibility with current DB/frontend values
    "elite": "scale",
}

#: Tiers that require money. `enterprise` is sold offline, so it never comes
#: out of a self-serve checkout, but it is still a paid entitlement.
PAID_PLANS: Final[frozenset[str]] = frozenset({"basic", "pro", "scale", "enterprise"})

#: Tiers that can be bought through Stripe Checkout without talking to sales.
SELF_SERVE_PLANS: Final[frozenset[str]] = frozenset({"basic", "pro", "scale"})

PLAN_SPECS: Final[dict[str, PlanSpec]] = {
    "free": PlanSpec(
        key="free",
        label="Free",
        seat_cap=3,
        included_storage_bytes=10 * GB_IN_BYTES,
        storage_addon_tb_price_usd=None,
        grace_days=7,
        ugc_credits_monthly=3,
    ),
    "basic": PlanSpec(
        key="basic",
        label="Basic",
        seat_cap=5,
        included_storage_bytes=250 * GB_IN_BYTES,
        # No storage add-on: overage on Basic is the reason to move to Pro.
        storage_addon_tb_price_usd=None,
        grace_days=7,
        ugc_credits_monthly=15,
    ),
    "pro": PlanSpec(
        key="pro",
        label="Pro",
        seat_cap=None,
        included_storage_bytes=2 * TB_IN_BYTES,
        storage_addon_tb_price_usd=8,
        grace_days=7,
        ugc_credits_monthly=50,
    ),
    "scale": PlanSpec(
        key="scale",
        label="Scale",
        seat_cap=None,
        included_storage_bytes=5 * TB_IN_BYTES,
        storage_addon_tb_price_usd=6,
        grace_days=7,
        ugc_credits_monthly=200,
    ),
    "enterprise": PlanSpec(
        key="enterprise",
        label="Enterprise",
        seat_cap=None,
        included_storage_bytes=20 * TB_IN_BYTES,
        storage_addon_tb_price_usd=None,
        grace_days=7,
        ugc_credits_monthly=1000,
    ),
}


def editube_product_prefixes() -> tuple[str, ...]:
    """Product-name prefixes that mark a Stripe product as ours.

    Overridable with `EDITUBE_STRIPE_PRODUCT_PREFIXES` (comma-separated) for
    deployments whose products are named differently.
    """
    raw = os.getenv("EDITUBE_STRIPE_PRODUCT_PREFIXES", "editube")
    return tuple(p.strip().lower() for p in raw.split(",") if p.strip())


def is_editube_product(name: str | None) -> bool | None:
    """True / False / None for "is this Stripe product ours?".

    `None` means the product's name is not known locally — usually a price
    webhook that referenced the product by id before the product itself was
    synced. That is deliberately distinct from a confident False, because
    refusing to map a genuine Editube price merely because its product name
    has not arrived yet would silently break checkout.

    This exists because one Stripe account serves several unrelated products.
    Matching on the product name is the durable rule; per-price metadata is
    what that rule is then expressed as.
    """
    if not name:
        return None
    lowered = name.strip().lower()
    return any(lowered.startswith(prefix) for prefix in editube_product_prefixes())


def resolve_plan_key(plan: str | None) -> str | None:
    """Canonical plan key, or None when the input names no known plan.

    The distinction matters wherever an absent plan and an unrecognised one
    should be handled differently. `normalize_plan_key` collapses both to
    "free", which is right for "what tier is this user on?" and wrong for
    "did the caller ask for a real plan?" — `PUT /onboarding/plan` used the
    collapsing version and so quietly downgraded anyone who sent "PRO".
    """
    if not plan:
        return None
    return PLAN_ALIASES.get(plan.strip().lower())


def normalize_plan_key(plan: str | None) -> str:
    """Canonical plan key, defaulting to "free" for anything unrecognised."""
    return resolve_plan_key(plan) or "free"


def get_plan_spec(plan: str | None) -> PlanSpec:
    return PLAN_SPECS[normalize_plan_key(plan)]
