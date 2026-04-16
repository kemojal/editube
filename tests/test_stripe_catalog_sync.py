import unittest
from unittest.mock import MagicMock

from app.db.models import StripeProduct
from app.services.stripe_catalog_sync import (
    parse_editube_mapping_from_price,
    resolve_checkout_price_id,
    upsert_stripe_price_from_object,
)


class ParseEditubeMappingTests(unittest.TestCase):
    def test_metadata_plan_and_interval(self) -> None:
        plan, iv = parse_editube_mapping_from_price(
            {
                "metadata": {"editube_plan": "scale", "editube_interval": "month"},
            }
        )
        self.assertEqual(plan, "scale")
        self.assertEqual(iv, "month")

    def test_metadata_aliases(self) -> None:
        plan, iv = parse_editube_mapping_from_price(
            {"metadata": {"plan": "PRO", "interval": "annual"}}
        )
        self.assertEqual(plan, "pro")
        self.assertEqual(iv, "year")

    def test_lookup_key_fills_gaps(self) -> None:
        plan, iv = parse_editube_mapping_from_price(
            {"metadata": {}, "lookup_key": "editube_pro_monthly"}
        )
        self.assertEqual(plan, "pro")
        self.assertEqual(iv, "month")

    def test_unknown_metadata_plan_normalizes_to_free(self) -> None:
        plan, iv = parse_editube_mapping_from_price(
            {"metadata": {"editube_plan": "nope", "editube_interval": "month"}}
        )
        self.assertEqual(plan, "free")
        self.assertEqual(iv, "month")

    def test_unrecognized_lookup_key_leaves_mapping_incomplete(self) -> None:
        plan, iv = parse_editube_mapping_from_price(
            {"metadata": {}, "lookup_key": "not_editube_format"}
        )
        self.assertIsNone(plan)
        self.assertIsNone(iv)


class ResolveCheckoutPriceIdTests(unittest.TestCase):
    def test_returns_matching_row_id(self) -> None:
        row = MagicMock()
        row.stripe_price_id = "price_abc"

        q = MagicMock()
        q.filter.return_value = q
        q.order_by.return_value = q
        q.first.return_value = row

        db = MagicMock()
        db.query.return_value = q

        self.assertEqual(
            resolve_checkout_price_id(db, plan="pro", interval="month"),
            "price_abc",
        )

    def test_none_when_no_row(self) -> None:
        q = MagicMock()
        q.filter.return_value = q
        q.order_by.return_value = q
        q.first.return_value = None
        db = MagicMock()
        db.query.return_value = q
        self.assertIsNone(resolve_checkout_price_id(db, plan="scale", interval="year"))


class UpsertStripePriceTests(unittest.TestCase):
    def test_upsert_is_idempotent_on_same_id(self) -> None:
        existing_price = MagicMock()
        existing_product = MagicMock()

        def query_side(model):
            mq = MagicMock()
            mq.filter.return_value = mq
            mq.first.return_value = (
                existing_product if model is StripeProduct else existing_price
            )
            return mq

        db = MagicMock()
        db.query.side_effect = query_side

        payload = {
            "id": "price_x",
            "product": "prod_y",
            "currency": "usd",
            "unit_amount": 2900,
            "nickname": "Pro",
            "active": True,
            "metadata": {"editube_plan": "pro", "editube_interval": "month"},
            "recurring": {"interval": "month"},
        }
        upsert_stripe_price_from_object(db, payload)
        self.assertEqual(existing_price.stripe_product_id, "prod_y")
        self.assertEqual(existing_price.currency, "usd")
        self.assertEqual(existing_price.unit_amount, 2900)
        self.assertEqual(existing_price.editube_plan, "pro")
        self.assertEqual(existing_price.editube_interval, "month")
        db.add.assert_not_called()

    def test_insert_when_missing(self) -> None:
        def query_side(model):
            mq = MagicMock()
            mq.filter.return_value = mq
            mq.first.return_value = None
            return mq

        db = MagicMock()
        db.query.side_effect = query_side

        payload = {
            "id": "price_new",
            "product": "prod_z",
            "currency": "usd",
            "unit_amount": 100,
            "active": True,
            "metadata": {"editube_plan": "scale", "editube_interval": "year"},
            "recurring": {"interval": "year"},
        }
        upsert_stripe_price_from_object(db, payload)
        self.assertEqual(db.add.call_count, 2)


if __name__ == "__main__":
    unittest.main()
