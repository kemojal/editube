#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

import stripe  # noqa: E402


COUPON_ID = "editube_pro_annual_launch_2026_25"
PUBLIC_CODE = "EDITUBE25"
END_AT = datetime(2026, 8, 5, 23, 59, 59, tzinfo=timezone.utc)


def main() -> int:
    secret = os.getenv("STRIPE_SECRET_KEY", "").strip()
    if not secret:
        print("STRIPE_SECRET_KEY is required", file=sys.stderr)
        return 1

    stripe.api_key = secret
    redeem_by = int(END_AT.timestamp())

    try:
        coupon = stripe.Coupon.retrieve(COUPON_ID)
    except stripe.InvalidRequestError:
        coupon = stripe.Coupon.create(
            id=COUPON_ID,
            name="Editube Pro annual launch offer",
            percent_off=25,
            duration="once",
            redeem_by=redeem_by,
            metadata={"campaign": "pro-annual-launch-2026"},
        )

    existing = stripe.PromotionCode.list(code=PUBLIC_CODE, active=True, limit=10)
    promotion_code = existing.data[0] if existing.data else stripe.PromotionCode.create(
        code=PUBLIC_CODE,
        promotion={"type": "coupon", "coupon": coupon.id},
        expires_at=redeem_by,
        max_redemptions=200,
        restrictions={"first_time_transaction": True},
        metadata={"campaign": "pro-annual-launch-2026"},
    )

    print(promotion_code.id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
