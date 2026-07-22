from __future__ import annotations

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
    "pro": "pro",
    "scale": "scale",
    "enterprise": "enterprise",
    # Backward compatibility with current DB/frontend values
    "basic": "free",
    "elite": "scale",
}

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


def normalize_plan_key(plan: str | None) -> str:
    if not plan:
        return "free"
    return PLAN_ALIASES.get(plan, "free")


def get_plan_spec(plan: str | None) -> PlanSpec:
    return PLAN_SPECS[normalize_plan_key(plan)]
