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

    def test_unknown_metadata_plan_maps_to_nothing(self) -> None:
        """An unrecognised plan name must not resolve to a real tier.

        This asserted `plan == "free"`, because `normalize_plan_key` collapses
        anything it does not recognise to "free". That is right for "what tier
        is this user on?" and wrong here: a typo in a Price's metadata
        (`editube_plan: "prro"`) silently mapped that Price to the Free tier
        and made it sellable at whatever it charges. Unrecognised now stays
        unrecognised.
        """
        plan, iv = parse_editube_mapping_from_price(
            {"metadata": {"editube_plan": "nope", "editube_interval": "month"}}
        )
        self.assertIsNone(plan)
        self.assertEqual(iv, "month")

    def test_basic_is_a_real_tier_not_an_alias_for_free(self) -> None:
        plan, iv = parse_editube_mapping_from_price(
            {"metadata": {"editube_plan": "basic", "editube_interval": "year"}}
        )
        self.assertEqual(plan, "basic")
        self.assertEqual(iv, "year")

    def test_basic_lookup_key_is_recognised(self) -> None:
        plan, iv = parse_editube_mapping_from_price(
            {"metadata": {}, "lookup_key": "editube_basic_monthly"}
        )
        self.assertEqual(plan, "basic")
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


def _sqlite_session():
    """A session with `autoflush=False`, matching `app.db.database.SessionLocal`.

    The default in tests is autoflush=True, which papers over exactly the bug
    below — the production session does not autoflush, so a row added but not
    flushed is invisible to the next query and absent when a dependent INSERT
    with a foreign key runs.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db.database import Base
    from tests.conftest import all_public_tables

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine, tables=all_public_tables())
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)()


def test_price_upsert_creates_its_product_before_the_price():
    """The catalog sync imported zero prices because of this.

    `stripe_prices.stripe_product_id` has an FK to `stripe_products`. With
    `autoflush=False` the product row stayed pending, so the price INSERT hit
    `ForeignKeyViolation` and every price was skipped.
    """
    from app.db.models import StripePrice, StripeProduct
    from app.services.stripe_catalog_sync import upsert_stripe_price_from_object

    db = _sqlite_session()
    try:
        upsert_stripe_price_from_object(db, {
            "id": "price_new",
            "product": "prod_never_seen",
            "currency": "usd",
            "unit_amount": 1099,
            "active": True,
            "recurring": {"interval": "month"},
            "metadata": {"editube_plan": "pro", "editube_interval": "month"},
        })
        db.commit()

        assert db.query(StripeProduct).filter(
            StripeProduct.stripe_product_id == "prod_never_seen"
        ).count() == 1
        assert db.query(StripePrice).filter(
            StripePrice.stripe_price_id == "price_new"
        ).one().editube_plan == "pro"
    finally:
        db.close()


def test_syncing_a_product_then_its_price_does_not_duplicate_the_product():
    """Without a flush, the second lookup missed the pending row and re-added it."""
    from app.db.models import StripeProduct
    from app.services.stripe_catalog_sync import (
        upsert_stripe_price_from_object,
        upsert_stripe_product_from_object,
    )

    db = _sqlite_session()
    try:
        upsert_stripe_product_from_object(db, {"id": "prod_1", "name": "Editube", "active": True})
        upsert_stripe_price_from_object(db, {
            "id": "price_1", "product": "prod_1", "currency": "usd",
            "unit_amount": 4900, "active": True,
            "recurring": {"interval": "month"},
            "metadata": {"editube_plan": "pro", "editube_interval": "month"},
        })
        db.commit()
        assert db.query(StripeProduct).filter(
            StripeProduct.stripe_product_id == "prod_1"
        ).count() == 1
    finally:
        db.close()


def test_stripe_object_to_dict_converts_nested_payloads():
    """`to_dict_recursive` was removed in stripe 15; the shallow fallback left
    nested `metadata` as a StripeObject the parser could not read."""
    from app.services.stripe_catalog_sync import stripe_object_to_dict
    from tests.test_billing_subscription_lifecycle import StripeObj

    obj = StripeObj({
        "id": "price_x",
        "product": StripeObj({"id": "prod_x", "name": "Editube"}),
        "recurring": StripeObj({"interval": "year"}),
        "metadata": StripeObj({"editube_plan": "scale"}),
    })
    out = stripe_object_to_dict(obj)
    assert isinstance(out, dict)
    assert isinstance(out["product"], dict) and out["product"]["id"] == "prod_x"
    assert isinstance(out["metadata"], dict)
    assert out["metadata"]["editube_plan"] == "scale"
    assert out["recurring"]["interval"] == "year"


class TestEditubeProductScoping:
    """Only products called "Editube" are ours.

    The Stripe account also carries Darlin, Axum, Rousel and others. Product
    name is the durable rule; per-price `editube_plan` metadata is how that
    rule gets expressed, and must not be able to override it.
    """

    def _sync(self, db, *, product_name, plan_meta="pro"):
        from app.services.stripe_catalog_sync import (
            upsert_stripe_price_from_object,
            upsert_stripe_product_from_object,
        )

        upsert_stripe_product_from_object(
            db, {"id": "prod_x", "name": product_name, "active": True}
        )
        upsert_stripe_price_from_object(db, {
            "id": "price_x", "product": "prod_x", "currency": "usd",
            "unit_amount": 999, "active": True,
            "recurring": {"interval": "month"},
            "metadata": {"editube_plan": plan_meta, "editube_interval": "month"},
        })
        db.commit()
        from app.db.models import StripePrice

        return db.query(StripePrice).filter(StripePrice.stripe_price_id == "price_x").one()

    def test_an_editube_product_maps_normally(self):
        db = _sqlite_session()
        try:
            assert self._sync(db, product_name="Editube Pro").editube_plan == "pro"
        finally:
            db.close()

    def test_another_products_price_cannot_map_itself(self):
        """A stray `editube_plan` key on a foreign price must be ignored."""
        db = _sqlite_session()
        try:
            assert self._sync(db, product_name="Darlin 3 Pro").editube_plan is None
        finally:
            db.close()

    def test_a_lookup_key_cannot_smuggle_a_foreign_price_in(self):
        from app.db.models import StripePrice
        from app.services.stripe_catalog_sync import (
            upsert_stripe_price_from_object,
            upsert_stripe_product_from_object,
        )

        db = _sqlite_session()
        try:
            upsert_stripe_product_from_object(
                db, {"id": "prod_o", "name": "Some Other App", "active": True}
            )
            upsert_stripe_price_from_object(db, {
                "id": "price_o", "product": "prod_o", "currency": "usd",
                "unit_amount": 999, "active": True,
                "recurring": {"interval": "month"},
                "lookup_key": "editube_pro_monthly",
                "metadata": {},
            })
            db.commit()
            row = db.query(StripePrice).filter(StripePrice.stripe_price_id == "price_o").one()
            assert row.editube_plan is None
        finally:
            db.close()

    def test_an_unknown_product_name_does_not_block_mapping(self):
        """A price webhook can arrive before its product's. Refusing then would
        break a genuine Editube price for a reason that resolves itself."""
        from app.db.models import StripePrice
        from app.services.stripe_catalog_sync import upsert_stripe_price_from_object

        db = _sqlite_session()
        try:
            upsert_stripe_price_from_object(db, {
                "id": "price_early", "product": "prod_unseen", "currency": "usd",
                "unit_amount": 1000, "active": True,
                "recurring": {"interval": "month"},
                "metadata": {"editube_plan": "pro", "editube_interval": "month"},
            })
            db.commit()
            row = db.query(StripePrice).filter(
                StripePrice.stripe_price_id == "price_early"
            ).one()
            assert row.editube_plan == "pro"
        finally:
            db.close()

    def test_the_prefix_list_is_configurable(self, monkeypatch):
        from app.services.pricing import is_editube_product

        monkeypatch.setenv("EDITUBE_STRIPE_PRODUCT_PREFIXES", "acme,editube")
        assert is_editube_product("Acme Pro") is True
        assert is_editube_product("Editube Pro") is True
        assert is_editube_product("Darlin 3 Pro") is False
        assert is_editube_product(None) is None
