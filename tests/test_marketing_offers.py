import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app.services.marketing_offers import active_marketing_offer, resolve_checkout_offer


OFFER_ENV = {
    "MARKETING_OFFER_ENABLED": "true",
    "MARKETING_OFFER_CAMPAIGN_ID": "pro-annual-launch-2026",
    "MARKETING_OFFER_PROMOTION_CODE_ID": "promo_test",
    "MARKETING_OFFER_PUBLIC_CODE": "EDITUBE25",
    "MARKETING_OFFER_PLAN": "pro",
    "MARKETING_OFFER_INTERVAL": "year",
    "MARKETING_OFFER_PERCENT_OFF": "25",
    "MARKETING_OFFER_END_AT": "2026-08-05T23:59:59Z",
    "MARKETING_OFFER_DURATION_LABEL": "first year",
}


class MarketingOfferTests(unittest.TestCase):
    def test_active_offer_exposes_only_public_fields(self):
        with patch.dict(os.environ, OFFER_ENV, clear=False):
            offer = active_marketing_offer(datetime(2026, 8, 1, tzinfo=timezone.utc))

        self.assertIsNotNone(offer)
        payload = offer.public_payload()
        self.assertEqual(payload["percent_off"], 25)
        self.assertNotIn("stripe_promotion_code_id", payload)

    def test_offer_expires_at_fixed_deadline(self):
        with patch.dict(os.environ, OFFER_ENV, clear=False):
            offer = active_marketing_offer(datetime(2026, 8, 6, tzinfo=timezone.utc))

        self.assertIsNone(offer)

    def test_checkout_requires_matching_campaign_plan_and_interval(self):
        now = datetime(2026, 8, 1, tzinfo=timezone.utc)
        with patch.dict(os.environ, OFFER_ENV, clear=False):
            valid = resolve_checkout_offer(
                "pro-annual-launch-2026",
                plan="pro",
                interval="year",
                now=now,
            )
            wrong_plan = resolve_checkout_offer(
                "pro-annual-launch-2026",
                plan="scale",
                interval="year",
                now=now,
            )

        self.assertIsNotNone(valid)
        self.assertIsNone(wrong_plan)


if __name__ == "__main__":
    unittest.main()
