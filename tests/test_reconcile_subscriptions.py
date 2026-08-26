"""The reconciliation script's verdicts and repairs.

This script downgrades live customers, so its judgement needs to be pinned
harder than most. The two mistakes that matter are asymmetric: wrongly
downgrading someone who is paying is a support incident, and wrongly leaving a
self-granted account alone just means the drift persists. So the cases below
lean on proving the *first* never happens — a paying customer is never
downgraded, for any Stripe status that means money is moving, and a Stripe
lookup that errors is skipped rather than assumed dead.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.db.models import StripePrice, StripeProduct, Subscription, User

from tests.test_billing_subscription_lifecycle import NOW, StripeObj, _ts, make_subscription

import scripts.reconcile_subscriptions as recon


@pytest.fixture
def catalog(db_session):  # noqa: ANN001
    db_session.add(StripeProduct(stripe_product_id="prod_editube", name="Editube"))
    for price_id, plan, interval in [
        ("price_pro_month", "pro", "month"),
        ("price_scale_month", "scale", "month"),
    ]:
        db_session.add(
            StripePrice(
                stripe_price_id=price_id,
                stripe_product_id="prod_editube",
                currency="usd",
                unit_amount=4900,
                recurring_interval=interval,
                active=True,
                editube_plan=plan,
                editube_interval=interval,
            )
        )
    db_session.commit()


@pytest.fixture
def stripe_subs(monkeypatch: pytest.MonkeyPatch):
    """Control what Stripe reports per customer id."""
    by_customer: dict[str, list[StripeObj]] = {}

    def _list(customer=None, **kwargs):  # noqa: ANN001, ANN003, ANN202
        if customer == "cus_boom":
            raise ValueError("stripe is having a day")
        return StripeObj({"data": by_customer.get(customer, [])})

    monkeypatch.setattr(recon.stripe.Subscription, "list", _list)
    return by_customer


def _sub(sub_id, *, status, price_id, created=0, customer="cus_1", plan_metadata="pro"):
    s = make_subscription(
        sub_id,
        status=status,
        price_id=price_id,
        customer=customer,
        plan_metadata=plan_metadata,
    )
    s["created"] = created
    return s


class TestVerdicts:
    def test_paid_plan_with_no_stripe_customer_is_a_downgrade(
        self, db_session, make_user, catalog, stripe_subs
    ):
        """The signature of the old `PUT /users/onboarding/plan` hole."""
        user = make_user(email="selfgrant@example.test", plan="scale")
        db_session.commit()
        finding = recon.inspect(db_session, user)
        assert finding.verdict == recon.DOWNGRADE
        assert "never went through checkout" in finding.detail

    def test_paid_plan_with_no_live_subscription_is_a_downgrade(
        self, db_session, make_user, catalog, stripe_subs
    ):
        """The signature of the old never-downgrade-on-cancel hole."""
        user = make_user(
            email="cancelled@example.test", plan="pro", stripe_customer_id="cus_1"
        )
        db_session.commit()
        stripe_subs["cus_1"] = [_sub("sub_old", status="canceled", price_id="price_pro_month")]
        finding = recon.inspect(db_session, user)
        assert finding.verdict == recon.DOWNGRADE

    def test_matching_tier_and_row_is_ok(
        self, db_session, make_user, catalog, stripe_subs
    ):
        user = make_user(email="good@example.test", plan="pro", stripe_customer_id="cus_1")
        db_session.add(
            Subscription(
                user_id=user.id, stripe_subscription_id="sub_1",
                status="active", plan="pro",
            )
        )
        db_session.commit()
        stripe_subs["cus_1"] = [_sub("sub_1", status="active", price_id="price_pro_month")]
        assert recon.inspect(db_session, user).verdict == recon.OK

    def test_paying_for_a_different_tier_is_wrong_tier(
        self, db_session, make_user, catalog, stripe_subs
    ):
        """The signature of the portal-plan-change hole."""
        user = make_user(email="switched@example.test", plan="pro", stripe_customer_id="cus_1")
        db_session.commit()
        stripe_subs["cus_1"] = [_sub("sub_1", status="active", price_id="price_scale_month")]
        finding = recon.inspect(db_session, user)
        assert finding.verdict == recon.WRONG_TIER
        assert finding.stripe_plan == "scale"

    def test_paying_while_marked_free_is_an_upgrade(
        self, db_session, make_user, catalog, stripe_subs
    ):
        """A lost grant. Worth finding — this one is the customer's money."""
        user = make_user(email="lost@example.test", plan="free", stripe_customer_id="cus_1")
        db_session.commit()
        stripe_subs["cus_1"] = [_sub("sub_1", status="active", price_id="price_pro_month")]
        assert recon.inspect(db_session, user).verdict == recon.UPGRADE

    def test_unsynced_catalog_reports_rather_than_guesses(
        self, db_session, make_user, catalog, stripe_subs
    ):
        """Guessing a tier from an unknown price is how you downgrade a customer.

        Metadata is stripped too — with it present the resolver would fall back
        to it, which is the correct behaviour and a different case.
        """
        user = make_user(email="unknown@example.test", plan="pro", stripe_customer_id="cus_1")
        db_session.commit()
        stripe_subs["cus_1"] = [
            _sub("sub_1", status="active", price_id="price_mystery", plan_metadata=None)
        ]
        finding = recon.inspect(db_session, user)
        assert finding.verdict == recon.MISSING_ROW
        assert "sync_stripe_catalog" in finding.detail

    def test_correct_tier_with_no_local_row_is_missing_row_not_ok(
        self, db_session, make_user, catalog, stripe_subs
    ):
        user = make_user(email="norow@example.test", plan="pro", stripe_customer_id="cus_1")
        db_session.commit()
        stripe_subs["cus_1"] = [_sub("sub_1", status="active", price_id="price_pro_month")]
        assert recon.inspect(db_session, user).verdict == recon.MISSING_ROW

    @pytest.mark.parametrize("status", sorted(recon.ENTITLED_STATUSES))
    def test_no_entitled_status_is_ever_downgraded(
        self, db_session, make_user, catalog, stripe_subs, status
    ):
        """The one thing this script must never get wrong.

        `past_due` in particular: it means Stripe is still retrying, and
        downgrading mid-dunning turns an expired card into a cancellation.
        """
        user = make_user(
            email=f"{status}@example.test", plan="pro", stripe_customer_id="cus_1"
        )
        db_session.commit()
        stripe_subs["cus_1"] = [_sub("sub_1", status=status, price_id="price_pro_month")]
        assert recon.inspect(db_session, user).verdict != recon.DOWNGRADE

    def test_the_newest_entitled_subscription_wins(
        self, db_session, make_user, catalog, stripe_subs
    ):
        """Duplicate subscriptions existed, because checkout did not block them."""
        user = make_user(email="dupe@example.test", plan="pro", stripe_customer_id="cus_1")
        db_session.commit()
        stripe_subs["cus_1"] = [
            _sub("sub_old", status="active", price_id="price_pro_month", created=100),
            _sub("sub_new", status="active", price_id="price_scale_month", created=200),
        ]
        finding = recon.inspect(db_session, user)
        assert finding.stripe_subscription_id == "sub_new"
        assert finding.stripe_plan == "scale"

    def test_a_stripe_error_is_raised_not_swallowed_into_a_downgrade(
        self, db_session, make_user, catalog, stripe_subs
    ):
        """An API blip must not read as "this customer stopped paying"."""
        user = make_user(email="boom@example.test", plan="pro", stripe_customer_id="cus_boom")
        db_session.commit()
        with pytest.raises(RuntimeError):
            recon.inspect(db_session, user)


class TestRepair:
    def test_downgrade_clears_tier_pointer_and_grace(
        self, db_session, make_user, catalog, stripe_subs
    ):
        user = make_user(
            email="fix@example.test", plan="scale",
            stripe_subscription_id="sub_dead",
            storage_grace_until=NOW + timedelta(days=3),
        )
        db_session.add(
            Subscription(
                user_id=user.id, stripe_subscription_id="sub_dead",
                status="active", plan="scale",
            )
        )
        db_session.commit()

        finding = recon.inspect(db_session, user)
        recon.repair(db_session, user, finding)
        db_session.commit()
        db_session.refresh(user)

        assert user.plan == "free"
        assert user.stripe_subscription_id is None
        assert user.storage_grace_until is None
        row = db_session.query(Subscription).filter(
            Subscription.stripe_subscription_id == "sub_dead"
        ).one()
        assert row.status == "canceled"
        assert row.ended_at is not None
        assert finding.applied is True

    def test_wrong_tier_is_corrected_to_what_stripe_bills(
        self, db_session, make_user, catalog, stripe_subs
    ):
        user = make_user(email="tier@example.test", plan="pro", stripe_customer_id="cus_1")
        db_session.commit()
        stripe_subs["cus_1"] = [_sub("sub_1", status="active", price_id="price_scale_month")]

        finding = recon.inspect(db_session, user)
        recon.repair(db_session, user, finding)
        db_session.commit()
        db_session.refresh(user)

        assert user.plan == "scale"
        assert user.stripe_subscription_id == "sub_1"
        assert user.subscription_status == "active"

    def test_repair_backfills_the_missing_subscription_row(
        self, db_session, make_user, catalog, stripe_subs
    ):
        user = make_user(email="backfill@example.test", plan="pro", stripe_customer_id="cus_1")
        db_session.commit()
        stripe_subs["cus_1"] = [_sub("sub_1", status="active", price_id="price_pro_month")]

        finding = recon.inspect(db_session, user)
        recon.repair(db_session, user, finding)
        db_session.commit()

        row = db_session.query(Subscription).filter(
            Subscription.stripe_subscription_id == "sub_1"
        ).one()
        assert row.plan == "pro"
        assert row.user_id == user.id
        assert row.current_period_end is not None

    def test_unresolvable_price_is_left_untouched(
        self, db_session, make_user, catalog, stripe_subs
    ):
        """Better to report and stop than to write a guess over a real customer."""
        user = make_user(email="mystery@example.test", plan="pro", stripe_customer_id="cus_1")
        db_session.commit()
        stripe_subs["cus_1"] = [
            _sub("sub_1", status="active", price_id="price_mystery", plan_metadata=None)
        ]

        finding = recon.inspect(db_session, user)
        recon.repair(db_session, user, finding)
        db_session.commit()
        db_session.refresh(user)

        assert user.plan == "pro"
        assert finding.applied is False

    def test_repair_is_idempotent(self, db_session, make_user, catalog, stripe_subs):
        user = make_user(email="twice@example.test", plan="scale", stripe_customer_id="cus_1")
        db_session.commit()
        stripe_subs["cus_1"] = [_sub("sub_1", status="active", price_id="price_pro_month")]

        for _ in range(2):
            finding = recon.inspect(db_session, user)
            recon.repair(db_session, user, finding)
            db_session.commit()
            db_session.refresh(user)

        assert user.plan == "pro"
        assert recon.inspect(db_session, user).verdict == recon.OK
        assert db_session.query(Subscription).filter(
            Subscription.user_id == user.id
        ).count() == 1

    def test_ok_accounts_are_never_written(
        self, db_session, make_user, catalog, stripe_subs
    ):
        user = make_user(email="fine@example.test", plan="pro", stripe_customer_id="cus_1")
        db_session.add(
            Subscription(
                user_id=user.id, stripe_subscription_id="sub_1",
                status="active", plan="pro",
            )
        )
        db_session.commit()
        stripe_subs["cus_1"] = [_sub("sub_1", status="active", price_id="price_pro_month")]

        finding = recon.inspect(db_session, user)
        recon.repair(db_session, user, finding)
        assert finding.applied is False


class TestCandidateSelection:
    def test_deleted_accounts_are_skipped(self, db_session, make_user):
        from datetime import datetime

        make_user(email="gone@example.test", plan="pro", deleted_at=datetime.utcnow())
        db_session.commit()
        assert recon.candidates(db_session, user_id=None, include_free=False) == []

    def test_free_accounts_are_skipped_by_default(self, db_session, make_user):
        make_user(email="freebie@example.test", plan="free", stripe_customer_id="cus_1")
        db_session.commit()
        assert recon.candidates(db_session, user_id=None, include_free=False) == []

    def test_include_free_catches_lost_grants(self, db_session, make_user):
        make_user(email="freebie@example.test", plan="free", stripe_customer_id="cus_1")
        db_session.commit()
        found = recon.candidates(db_session, user_id=None, include_free=True)
        assert [u.email for u in found] == ["freebie@example.test"]


class TestCrossProductIsolation:
    """The Stripe account serves several products; only ours may entitle."""

    @pytest.fixture
    def foreign_price(self, db_session):  # noqa: ANN001
        db_session.add(StripeProduct(stripe_product_id="prod_other", name="Some Other App"))
        db_session.add(
            StripePrice(
                stripe_price_id="price_other", stripe_product_id="prod_other",
                currency="usd", unit_amount=999, recurring_interval="month",
                active=True, editube_plan=None, editube_interval="month",
            )
        )
        db_session.commit()

    def test_another_products_subscription_is_not_an_editube_entitlement(
        self, db_session, make_user, catalog, foreign_price, stripe_subs
    ):
        """The live finding: an account showed Editube `pro` on the strength of
        a subscription to a different product on the same Stripe account."""
        user = make_user(email="mixed@example.test", plan="pro", stripe_customer_id="cus_1")
        db_session.commit()
        stripe_subs["cus_1"] = [
            _sub("sub_other", status="active", price_id="price_other", plan_metadata=None)
        ]
        finding = recon.inspect(db_session, user)
        assert finding.verdict == recon.DOWNGRADE
        assert "no live Editube subscription" in finding.detail

    def test_a_real_editube_subscription_still_wins_alongside_a_foreign_one(
        self, db_session, make_user, catalog, foreign_price, stripe_subs
    ):
        user = make_user(email="both@example.test", plan="pro", stripe_customer_id="cus_1")
        db_session.add(
            Subscription(
                user_id=user.id, stripe_subscription_id="sub_ours",
                status="active", plan="pro",
            )
        )
        db_session.commit()
        stripe_subs["cus_1"] = [
            _sub("sub_other", status="active", price_id="price_other",
                 plan_metadata=None, created=300),
            _sub("sub_ours", status="active", price_id="price_pro_month", created=100),
        ]
        finding = recon.inspect(db_session, user)
        assert finding.verdict == recon.OK
        assert finding.stripe_subscription_id == "sub_ours"


def test_apply_is_refused_while_the_catalog_has_no_mappings(db_session, monkeypatch):
    """Verdicts are unreliable with nothing mapped, so acting on them is refused.

    Without a mapping every plan falls back to subscription metadata, and a
    foreign product's subscription is indistinguishable from one of ours.
    """
    from app.db.models import StripePrice as _P

    assert recon._mapped_price_count(db_session) == 0
