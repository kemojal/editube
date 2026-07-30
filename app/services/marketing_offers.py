from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone


def _parse_utc(value: str) -> datetime | None:
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class MarketingOffer:
    campaign_id: str
    stripe_promotion_code_id: str
    public_code: str
    plan: str
    interval: str
    percent_off: int
    end_at: datetime
    duration_label: str

    def is_active(self, now: datetime | None = None) -> bool:
        current = now or datetime.now(timezone.utc)
        return current < self.end_at

    def is_eligible(self, *, plan: str, interval: str) -> bool:
        return plan == self.plan and interval == self.interval

    def public_payload(self) -> dict[str, object]:
        return {
            "campaign_id": self.campaign_id,
            "public_code": self.public_code,
            "plan": self.plan,
            "interval": self.interval,
            "percent_off": self.percent_off,
            "end_at": self.end_at.isoformat().replace("+00:00", "Z"),
            "duration_label": self.duration_label,
        }


def configured_marketing_offer() -> MarketingOffer | None:
    enabled = os.getenv("MARKETING_OFFER_ENABLED", "").strip().lower()
    if enabled not in {"1", "true", "yes"}:
        return None

    campaign_id = os.getenv("MARKETING_OFFER_CAMPAIGN_ID", "").strip()
    promotion_code_id = os.getenv("MARKETING_OFFER_PROMOTION_CODE_ID", "").strip()
    end_at = _parse_utc(os.getenv("MARKETING_OFFER_END_AT", ""))
    try:
        percent_off = int(os.getenv("MARKETING_OFFER_PERCENT_OFF", "0"))
    except ValueError:
        return None

    if not campaign_id or not promotion_code_id or end_at is None or not 1 <= percent_off <= 100:
        return None

    return MarketingOffer(
        campaign_id=campaign_id,
        stripe_promotion_code_id=promotion_code_id,
        public_code=os.getenv("MARKETING_OFFER_PUBLIC_CODE", "").strip(),
        plan=os.getenv("MARKETING_OFFER_PLAN", "pro").strip().lower(),
        interval=os.getenv("MARKETING_OFFER_INTERVAL", "year").strip().lower(),
        percent_off=percent_off,
        end_at=end_at,
        duration_label=os.getenv("MARKETING_OFFER_DURATION_LABEL", "first billing term").strip(),
    )


def active_marketing_offer(now: datetime | None = None) -> MarketingOffer | None:
    offer = configured_marketing_offer()
    if offer is None or not offer.is_active(now):
        return None
    return offer


def resolve_checkout_offer(
    campaign_id: str | None,
    *,
    plan: str,
    interval: str,
    now: datetime | None = None,
) -> MarketingOffer | None:
    if not campaign_id:
        return None
    offer = active_marketing_offer(now)
    if offer is None or offer.campaign_id != campaign_id or not offer.is_eligible(plan=plan, interval=interval):
        return None
    return offer
