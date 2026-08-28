"""The subscription lifecycle, end to end, with Stripe faked at the SDK boundary.

There was no coverage of billing at all before this file, which is how a
collection of fairly serious holes survived: cancelling never dropped the
customer's tier, the free trial renewed itself if you cancelled and bought
again, a plan changed in the Stripe billing portal was ignored, and any
authenticated user could award themselves Scale quotas with one PUT.

Every test below names the specific hole it pins shut. `FakeStripe` models the
parts of the API the code actually touches, at the API version the pinned SDK
speaks (`current_period_*` on subscription *items*, not on the subscription) —
that detail is itself one of the bugs.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.db.models import (
    CheckoutAttempt,
    StripePrice,
    StripeProduct,
    Subscription,
    User,
    Workspace,
    WorkspaceInvite,
    WorkspaceMember,
)


# --- Stripe fake --------------------------------------------------------------


def _ts(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


NOW = datetime(2026, 8, 16, 12, 0, 0)


class StripeObj:
    """A faithful stand-in for `stripe.StripeObject`.

    Deliberately **not** a dict subclass, and deliberately without `.get()`.
    Both of those are true of the real thing in stripe>=12, and the earlier
    version of this fake got them wrong — it subclassed dict, so `obj.get(...)`
    worked in tests and raised `AttributeError: get` against the live SDK. That
    single divergence hid a dead webhook: every handler branch opened with
    `session.get("mode")` or `sub.get("id")`, so every Stripe delivery 500'd in
    production while the suite stayed green.

    Missing keys raise `AttributeError` through `__getattr__`, matching
    `StripeObject`, so `getattr(obj, key, None)` is the only safe accessor —
    which is what `entitlements.stripe_field` does.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        object.__setattr__(self, "_data", dict(data))

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __getattr__(self, key: str) -> Any:
        if key.startswith("_"):
            raise AttributeError(key)
        try:
            return self._data[key]
        except KeyError as exc:
            raise AttributeError(key) from exc

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"StripeObj({self._data!r})"


def make_subscription(
    sub_id: str = "sub_1",
    *,
    status: str = "active",
    price_id: str = "price_pro_month",
    customer: str = "cus_1",
    plan_metadata: str | None = "pro",
    user_id: int | None = None,
    cancel_at_period_end: bool = False,
    trial_start: datetime | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> StripeObj:
    """A subscription shaped the way stripe>=15 returns one.

    The billing period lives on the *item*. Reading it off the subscription is
    what the production code used to do, and it silently returned None on every
    modern API version.
    """
    metadata: dict[str, str] = {}
    if plan_metadata is not None:
        metadata["plan"] = plan_metadata
    if user_id is not None:
        metadata["user_id"] = str(user_id)

    item = StripeObj(
        {
            "id": f"si_{sub_id}",
            "price": StripeObj({"id": price_id}),
            "current_period_start": _ts(period_start or NOW),
            "current_period_end": _ts(period_end or (NOW + timedelta(days=30))),
        }
    )
    return StripeObj(
        {
            "id": sub_id,
            "object": "subscription",
            "status": status,
            "customer": customer,
            "metadata": metadata,
            "cancel_at_period_end": cancel_at_period_end,
            "trial_start": _ts(trial_start) if trial_start else None,
            "items": StripeObj({"object": "list", "data": [item]}),
        }
    )


class FakeStripe:
    """Records what was sent to Stripe and replays canned subscriptions back."""

    def __init__(self) -> None:
        self.subscriptions: dict[str, StripeObj] = {}
        self.checkout_calls: list[dict[str, Any]] = []
        self.customers_created: list[dict[str, Any]] = []
        self.portal_calls: list[dict[str, Any]] = []
        self.next_customer_id = "cus_new"

    def add(self, sub: StripeObj) -> StripeObj:
        self.subscriptions[sub["id"]] = sub
        return sub


@pytest.fixture
def fake_stripe(monkeypatch: pytest.MonkeyPatch) -> FakeStripe:
    import app.api.routes.billing as billing

    fake = FakeStripe()
    monkeypatch.setattr(billing.stripe, "api_key", "sk_test_fake")

    def _retrieve(sub_id, **kwargs):  # noqa: ANN001, ANN202
        if sub_id not in fake.subscriptions:
            raise AssertionError(f"test asked Stripe for unknown subscription {sub_id}")
        return fake.subscriptions[sub_id]

    def _session_create(**kwargs):  # noqa: ANN003, ANN202
        fake.checkout_calls.append(kwargs)
        return StripeObj({"id": "cs_test_1", "url": "https://checkout.stripe.test/cs_test_1"})

    def _customer_create(**kwargs):  # noqa: ANN003, ANN202
        fake.customers_created.append(kwargs)
        return StripeObj({"id": fake.next_customer_id})

    def _portal_create(**kwargs):  # noqa: ANN003, ANN202
        fake.portal_calls.append(kwargs)
        return StripeObj({"url": "https://portal.stripe.test/x"})

    monkeypatch.setattr(billing.stripe.Subscription, "retrieve", _retrieve)
    monkeypatch.setattr(billing.stripe.checkout.Session, "create", _session_create)
    monkeypatch.setattr(billing.stripe.Customer, "create", _customer_create)
    monkeypatch.setattr(billing.stripe.billing_portal.Session, "create", _portal_create)
    return fake


@pytest.fixture
def emails(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Capture the transactional emails billing sends, per kind."""
    import app.api.routes.billing as billing

    sent: dict[str, list] = {
        "welcome": [],
        "canceled": [],
        "will_not_renew": [],
        "trial_ending": [],
        "payment_failed": [],
    }
    monkeypatch.setattr(
        billing, "send_subscription_welcome_email",
        lambda *a, **k: sent["welcome"].append((a, k)),
    )
    monkeypatch.setattr(
        billing, "send_subscription_canceled_email",
        lambda *a, **k: sent["canceled"].append((a, k)),
    )
    monkeypatch.setattr(
        billing, "send_subscription_will_not_renew_email",
        lambda *a, **k: sent["will_not_renew"].append((a, k)),
    )
    monkeypatch.setattr(
        billing, "send_trial_ending_email",
        lambda *a, **k: sent["trial_ending"].append((a, k)),
    )
    monkeypatch.setattr(
        billing, "send_payment_failed_email",
        lambda *a, **k: sent["payment_failed"].append((a, k)),
    )
    return sent


@pytest.fixture
def webhook(monkeypatch: pytest.MonkeyPatch, api_client):  # noqa: ANN001
    """Post a signed-looking Stripe event, with verification stubbed out."""
    import app.api.routes.billing as billing

    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    counter = {"n": 0}

    def _post(event_type: str, obj: dict, *, event_id: str | None = None):  # noqa: ANN202
        counter["n"] += 1
        eid = event_id or f"evt_{counter['n']}"
        # Shaped exactly as `stripe.Webhook.construct_event` returns it: an
        # Event StripeObject whose nested `data.object` is another one. Passing
        # plain dicts here is what let the dead-webhook bug through.
        payload = obj if isinstance(obj, StripeObj) else StripeObj(obj)
        event = StripeObj(
            {"id": eid, "type": event_type, "data": StripeObj({"object": payload})}
        )
        monkeypatch.setattr(
            billing.stripe.Webhook, "construct_event", lambda *a, **k: event
        )
        return api_client.post(
            "/billing/webhook",
            content=json.dumps({"id": eid}),
            headers={"stripe-signature": "t=1,v1=fake"},
        )

    return _post


@pytest.fixture
def catalog(db_session):  # noqa: ANN001
    """Pro and Scale prices, monthly and yearly, mapped for checkout."""
    db_session.add(StripeProduct(stripe_product_id="prod_editube", name="Editube"))
    rows = [
        ("price_pro_month", "pro", "month", 4900),
        ("price_pro_year", "pro", "year", 49000),
        ("price_scale_month", "scale", "month", 9900),
        ("price_scale_year", "scale", "year", 99000),
    ]
    for price_id, plan, interval, amount in rows:
        db_session.add(
            StripePrice(
                stripe_price_id=price_id,
                stripe_product_id="prod_editube",
                currency="usd",
                unit_amount=amount,
                recurring_interval=interval,
                active=True,
                editube_plan=plan,
                editube_interval=interval,
            )
        )
    db_session.commit()


@pytest.fixture
def subscriber(make_user, db_session):  # noqa: ANN001
    # `onboarding_completed` is set explicitly because the column's
    # `server_default="false"` is emitted as the SQL *string* "false", which
    # SQLite stores and reads back as truthy. Postgres is unaffected.
    user = make_user(
        email="paid@example.test",
        plan="free",
        stripe_customer_id="cus_1",
        onboarding_completed=False,
    )
    db_session.commit()
    return user


# --- Checkout ----------------------------------------------------------------


class TestCheckout:
    def test_first_checkout_grants_a_trial(
        self, api_client, subscriber, catalog, fake_stripe
    ):
        resp = api_client.login(subscriber).post(
            "/billing/checkout", json={"plan": "pro", "interval": "month"}
        )
        assert resp.status_code == 200
        options = fake_stripe.checkout_calls[-1]
        assert options["subscription_data"]["trial_period_days"] == 14
        assert options["line_items"][0]["price"] == "price_pro_month"

    def test_second_trial_is_refused_after_cancelling_the_first(
        self, api_client, subscriber, catalog, fake_stripe, db_session
    ):
        """The loop: subscribe, cancel inside 14 days, subscribe again, forever.

        Nothing recorded that the account had already had its trial, so every
        new subscription started another one and the service was free to
        anyone willing to click twice a fortnight.
        """
        db_session.add(
            Subscription(
                user_id=subscriber.id,
                stripe_subscription_id="sub_old",
                status="canceled",
                plan="pro",
                trial_start=NOW - timedelta(days=40),
                ended_at=NOW - timedelta(days=26),
            )
        )
        db_session.commit()

        resp = api_client.login(subscriber).post(
            "/billing/checkout", json={"plan": "pro", "interval": "month"}
        )
        assert resp.status_code == 200
        options = fake_stripe.checkout_calls[-1]
        assert "trial_period_days" not in options["subscription_data"], (
            "a resubscribing account was handed a second free trial"
        )

    def test_checkout_is_refused_while_a_subscription_is_live(
        self, api_client, subscriber, catalog, fake_stripe, db_session
    ):
        """Buying twice created two Stripe subscriptions and billed for both."""
        db_session.add(
            Subscription(
                user_id=subscriber.id,
                stripe_subscription_id="sub_live",
                status="active",
                plan="pro",
            )
        )
        db_session.commit()

        resp = api_client.login(subscriber).post(
            "/billing/checkout", json={"plan": "scale", "interval": "month"}
        )
        assert resp.status_code == 409
        assert not fake_stripe.checkout_calls

    def test_cancelled_subscription_does_not_block_resubscribing(
        self, api_client, subscriber, catalog, fake_stripe, db_session
    ):
        db_session.add(
            Subscription(
                user_id=subscriber.id,
                stripe_subscription_id="sub_dead",
                status="canceled",
                plan="pro",
            )
        )
        db_session.commit()

        resp = api_client.login(subscriber).post(
            "/billing/checkout", json={"plan": "pro", "interval": "month"}
        )
        assert resp.status_code == 200

    def test_unknown_plan_is_rejected_not_silently_downgraded(
        self, api_client, subscriber, catalog, fake_stripe
    ):
        resp = api_client.login(subscriber).post(
            "/billing/checkout", json={"plan": "platinum", "interval": "month"}
        )
        assert resp.status_code == 400

    def test_free_plan_cannot_be_bought(self, api_client, subscriber, catalog, fake_stripe):
        resp = api_client.login(subscriber).post(
            "/billing/checkout", json={"plan": "free", "interval": "month"}
        )
        assert resp.status_code == 400

    def test_return_path_cannot_escape_the_origin(
        self, api_client, subscriber, catalog, fake_stripe
    ):
        resp = api_client.login(subscriber).post(
            "/billing/checkout",
            json={
                "plan": "pro",
                "interval": "month",
                "return_path": "//evil.example.com/steal",
            },
        )
        assert resp.status_code == 200
        options = fake_stripe.checkout_calls[-1]
        assert "evil.example.com" not in options["cancel_url"]
        assert "evil.example.com" not in options["success_url"]

    def test_return_path_is_honoured_when_it_is_a_local_path(
        self, api_client, subscriber, catalog, fake_stripe
    ):
        """Upgrading from account settings used to dump the user in onboarding."""
        resp = api_client.login(subscriber).post(
            "/billing/checkout",
            json={"plan": "pro", "interval": "month", "return_path": "/dashboard?account=billing"},
        )
        assert resp.status_code == 200
        assert "/dashboard?account=billing" in fake_stripe.checkout_calls[-1]["cancel_url"]

    def test_cancel_return_closes_latest_open_checkout_attempt(
        self, api_client, subscriber, catalog, fake_stripe, db_session
    ):
        client = api_client.login(subscriber)
        created = client.post(
            "/billing/checkout", json={"plan": "pro", "interval": "month"}
        )
        assert created.status_code == 200
        attempt = db_session.query(CheckoutAttempt).filter_by(user_id=subscriber.id).one()
        assert attempt.status == "created"

        canceled = client.post("/billing/checkout-canceled", json={})

        assert canceled.status_code == 200
        assert canceled.json() == {"recorded": True}
        db_session.refresh(attempt)
        assert attempt.status == "canceled"
        assert attempt.canceled_at is not None

        repeated = client.post("/billing/checkout-canceled", json={})
        assert repeated.status_code == 200
        assert repeated.json() == {"recorded": False}

    def test_checkout_requires_authentication(self, api_client, catalog, fake_stripe):
        resp = api_client.logout().post(
            "/billing/checkout", json={"plan": "pro", "interval": "month"}
        )
        assert resp.status_code == 401
        assert api_client.post("/billing/checkout-canceled", json={}).status_code == 401


# --- Provisioning: what a completed checkout grants ---------------------------


class TestProvisioning:
    def test_completed_checkout_grants_the_plan(
        self, api_client, subscriber, catalog, fake_stripe, webhook, emails, db_session
    ):
        fake_stripe.add(
            make_subscription(
                "sub_1", status="trialing", trial_start=NOW, user_id=subscriber.id
            )
        )
        resp = webhook(
            "checkout.session.completed",
            {
                "id": "cs_1",
                "mode": "subscription",
                "subscription": "sub_1",
                "client_reference_id": str(subscriber.id),
                "metadata": {"user_id": str(subscriber.id), "plan": "pro"},
            },
        )
        assert resp.status_code == 200
        db_session.refresh(subscriber)
        assert subscriber.plan == "pro"
        assert subscriber.subscription_status == "trialing"
        assert subscriber.onboarding_completed is True
        assert len(emails["welcome"]) == 1

    def test_incomplete_subscription_grants_nothing(
        self, api_client, subscriber, catalog, fake_stripe, webhook, emails, db_session
    ):
        """A checkout whose first payment never cleared used to grant Pro anyway.

        `_sync_user_from_subscription` wrote `user.plan` for every status, and
        nothing in the app gates on `subscription_status`, so an `incomplete`
        subscription was indistinguishable from a paid one.
        """
        fake_stripe.add(make_subscription("sub_bad", status="incomplete", user_id=subscriber.id))
        webhook(
            "checkout.session.completed",
            {
                "id": "cs_2",
                "mode": "subscription",
                "subscription": "sub_bad",
                "client_reference_id": str(subscriber.id),
                "metadata": {"user_id": str(subscriber.id), "plan": "pro"},
            },
        )
        db_session.refresh(subscriber)
        assert subscriber.plan == "free"
        assert subscriber.onboarding_completed is False
        assert emails["welcome"] == []

    def test_billing_period_is_read_from_the_subscription_item(
        self, api_client, subscriber, catalog, fake_stripe, webhook, emails, db_session
    ):
        """Stripe moved these onto items in 2025-03-31.basil.

        Reading them off the subscription returned None on every API version
        the pinned SDK speaks, so the billing panel never showed a renewal date
        and the cancellation email never said when access ended.
        """
        end = NOW + timedelta(days=30)
        fake_stripe.add(
            make_subscription("sub_1", user_id=subscriber.id, period_start=NOW, period_end=end)
        )
        webhook(
            "customer.subscription.updated",
            {"id": "sub_1", "metadata": {"user_id": str(subscriber.id)}},
        )
        row = (
            db_session.query(Subscription)
            .filter(Subscription.stripe_subscription_id == "sub_1")
            .one()
        )
        assert row.current_period_end is not None
        assert row.current_period_end.date() == end.date()

    def test_unresolvable_plan_does_not_revoke_a_paying_customer(
        self, api_client, subscriber, catalog, fake_stripe, webhook, emails, db_session
    ):
        """An unsynced catalog is a config problem, not grounds for a downgrade."""
        subscriber.plan = "pro"
        subscriber.stripe_subscription_id = "sub_1"
        db_session.commit()
        fake_stripe.add(
            make_subscription(
                "sub_1", price_id="price_not_in_catalog", plan_metadata=None,
                user_id=subscriber.id,
            )
        )
        webhook(
            "customer.subscription.updated",
            {"id": "sub_1", "metadata": {"user_id": str(subscriber.id)}},
        )
        db_session.refresh(subscriber)
        assert subscriber.plan == "pro"

    def test_subscription_row_records_the_purchased_plan_without_a_hint(
        self, api_client, subscriber, catalog, fake_stripe, webhook, db_session
    ):
        """`normalize_plan_key(None)` returns "free", not None.

        So the old resolution chain short-circuited on its first step and every
        row synced without an explicit hint was stamped `plan="free"` — the
        metadata and catalog lookups after it were unreachable.
        """
        fake_stripe.add(
            make_subscription(
                "sub_1", price_id="price_scale_month", plan_metadata=None,
                user_id=subscriber.id,
            )
        )
        webhook(
            "customer.subscription.updated",
            {"id": "sub_1", "metadata": {"user_id": str(subscriber.id)}},
        )
        row = (
            db_session.query(Subscription)
            .filter(Subscription.stripe_subscription_id == "sub_1")
            .one()
        )
        assert row.plan == "scale"


# --- Plan changes made in the Stripe billing portal ---------------------------


class TestPortalPlanChange:
    def _establish_pro(self, subscriber, fake_stripe, webhook, db_session):
        fake_stripe.add(make_subscription("sub_1", price_id="price_pro_month", user_id=subscriber.id))
        webhook(
            "checkout.session.completed",
            {
                "id": "cs_1",
                "mode": "subscription",
                "subscription": "sub_1",
                "client_reference_id": str(subscriber.id),
                "metadata": {"user_id": str(subscriber.id), "plan": "pro"},
            },
        )
        db_session.refresh(subscriber)
        assert subscriber.plan == "pro"

    def test_upgrade_in_portal_takes_effect(
        self, api_client, subscriber, catalog, fake_stripe, webhook, emails, db_session
    ):
        """The customer pays Scale and gets it.

        Metadata is stamped once at checkout and never updated, so reading the
        plan from it meant a portal upgrade changed the invoice and nothing
        else — Scale money, Pro caps.
        """
        self._establish_pro(subscriber, fake_stripe, webhook, db_session)

        # Portal swaps the price; metadata still says "pro".
        fake_stripe.subscriptions["sub_1"] = make_subscription(
            "sub_1", price_id="price_scale_month", plan_metadata="pro", user_id=subscriber.id
        )
        webhook(
            "customer.subscription.updated",
            {"id": "sub_1", "metadata": {"user_id": str(subscriber.id)}},
        )
        db_session.refresh(subscriber)
        assert subscriber.plan == "scale"

    def test_downgrade_in_portal_takes_effect(
        self, api_client, subscriber, catalog, fake_stripe, webhook, emails, db_session
    ):
        """And the reverse: Pro money must not keep Scale caps."""
        fake_stripe.add(
            make_subscription("sub_1", price_id="price_scale_month", plan_metadata="scale",
                              user_id=subscriber.id)
        )
        webhook(
            "checkout.session.completed",
            {
                "id": "cs_1", "mode": "subscription", "subscription": "sub_1",
                "client_reference_id": str(subscriber.id),
                "metadata": {"user_id": str(subscriber.id), "plan": "scale"},
            },
        )
        db_session.refresh(subscriber)
        assert subscriber.plan == "scale"

        fake_stripe.subscriptions["sub_1"] = make_subscription(
            "sub_1", price_id="price_pro_month", plan_metadata="scale", user_id=subscriber.id
        )
        webhook(
            "customer.subscription.updated",
            {"id": "sub_1", "metadata": {"user_id": str(subscriber.id)}},
        )
        db_session.refresh(subscriber)
        assert subscriber.plan == "pro"


# --- Cancellation -------------------------------------------------------------


class TestCancellation:
    def _establish_pro(self, subscriber, fake_stripe, webhook, db_session, sub_id="sub_1"):
        fake_stripe.add(make_subscription(sub_id, user_id=subscriber.id))
        webhook(
            "checkout.session.completed",
            {
                "id": f"cs_{sub_id}", "mode": "subscription", "subscription": sub_id,
                "client_reference_id": str(subscriber.id),
                "metadata": {"user_id": str(subscriber.id), "plan": "pro"},
            },
        )
        db_session.refresh(subscriber)

    def test_cancel_at_period_end_keeps_access_and_warns_once(
        self, api_client, subscriber, catalog, fake_stripe, webhook, emails, db_session
    ):
        self._establish_pro(subscriber, fake_stripe, webhook, db_session)
        fake_stripe.subscriptions["sub_1"] = make_subscription(
            "sub_1", user_id=subscriber.id, cancel_at_period_end=True
        )
        webhook(
            "customer.subscription.updated",
            {"id": "sub_1", "metadata": {"user_id": str(subscriber.id)}},
        )
        db_session.refresh(subscriber)
        assert subscriber.plan == "pro", "access must last to the end of the paid period"
        assert len(emails["will_not_renew"]) == 1

        # A second update with the flag unchanged must not re-send the warning.
        webhook(
            "customer.subscription.updated",
            {"id": "sub_1", "metadata": {"user_id": str(subscriber.id)}},
        )
        assert len(emails["will_not_renew"]) == 1

    def test_deletion_drops_the_customer_to_free(
        self, api_client, subscriber, catalog, fake_stripe, webhook, emails, db_session
    ):
        """The big one.

        `customer.subscription.deleted` cleared `subscription_status` and left
        `user.plan` on "pro". Since every quota in the app reads `user.plan`
        and nothing reads `subscription_status`, cancelling bought a permanent
        Pro storage cap, Pro UGC credits and Pro seats, for free.
        """
        self._establish_pro(subscriber, fake_stripe, webhook, db_session)
        assert subscriber.plan == "pro"

        webhook(
            "customer.subscription.deleted",
            {"id": "sub_1", "status": "canceled", "customer": "cus_1",
             "metadata": {"user_id": str(subscriber.id)}},
        )
        db_session.refresh(subscriber)
        assert subscriber.plan == "free"
        assert subscriber.subscription_status == "canceled"
        assert subscriber.stripe_subscription_id is None
        assert len(emails["canceled"]) == 1

    def test_cancellation_email_knows_when_access_ends(
        self, api_client, subscriber, catalog, fake_stripe, webhook, emails, db_session
    ):
        self._establish_pro(subscriber, fake_stripe, webhook, db_session)
        webhook(
            "customer.subscription.deleted",
            {"id": "sub_1", "status": "canceled", "customer": "cus_1",
             "metadata": {"user_id": str(subscriber.id)}},
        )
        _, kwargs = emails["canceled"][0]
        assert kwargs["access_until"] is not None

    def test_unpaid_subscription_loses_access(
        self, api_client, subscriber, catalog, fake_stripe, webhook, emails, db_session
    ):
        """`unpaid` is where Stripe gives up on dunning."""
        self._establish_pro(subscriber, fake_stripe, webhook, db_session)
        fake_stripe.subscriptions["sub_1"] = make_subscription(
            "sub_1", status="unpaid", user_id=subscriber.id
        )
        webhook(
            "customer.subscription.updated",
            {"id": "sub_1", "metadata": {"user_id": str(subscriber.id)}},
        )
        db_session.refresh(subscriber)
        assert subscriber.plan == "free"

    def test_past_due_keeps_access_while_stripe_retries(
        self, api_client, subscriber, catalog, fake_stripe, webhook, emails, db_session
    ):
        """Deliberate: pulling storage over one expired card is churn, not policy."""
        self._establish_pro(subscriber, fake_stripe, webhook, db_session)
        fake_stripe.subscriptions["sub_1"] = make_subscription(
            "sub_1", status="past_due", user_id=subscriber.id
        )
        webhook(
            "customer.subscription.updated",
            {"id": "sub_1", "metadata": {"user_id": str(subscriber.id)}},
        )
        db_session.refresh(subscriber)
        assert subscriber.plan == "pro"
        assert subscriber.subscription_status == "past_due"

    def test_storage_grace_is_reset_on_downgrade(
        self, api_client, subscriber, catalog, fake_stripe, webhook, emails, db_session
    ):
        """A grace window is an allowance against one specific cap.

        Carrying it across a tier change is wrong both ways round: burn it on
        Free and you arrive at Pro with none left.
        """
        self._establish_pro(subscriber, fake_stripe, webhook, db_session)
        subscriber.storage_grace_until = NOW + timedelta(days=3)
        db_session.commit()

        webhook(
            "customer.subscription.deleted",
            {"id": "sub_1", "status": "canceled", "customer": "cus_1",
             "metadata": {"user_id": str(subscriber.id)}},
        )
        db_session.refresh(subscriber)
        assert subscriber.storage_grace_until is None


# --- Resubscription -----------------------------------------------------------


class TestResubscription:
    def test_resubscribing_restores_access_without_a_new_trial(
        self, api_client, subscriber, catalog, fake_stripe, webhook, emails, db_session
    ):
        fake_stripe.add(make_subscription("sub_1", status="trialing", trial_start=NOW,
                                          user_id=subscriber.id))
        webhook(
            "checkout.session.completed",
            {"id": "cs_1", "mode": "subscription", "subscription": "sub_1",
             "client_reference_id": str(subscriber.id),
             "metadata": {"user_id": str(subscriber.id), "plan": "pro"}},
        )
        webhook(
            "customer.subscription.deleted",
            {"id": "sub_1", "status": "canceled", "customer": "cus_1",
             "metadata": {"user_id": str(subscriber.id)}},
        )
        db_session.refresh(subscriber)
        assert subscriber.plan == "free"

        # Second purchase: no trial offered.
        resp = api_client.login(subscriber).post(
            "/billing/checkout", json={"plan": "pro", "interval": "month"}
        )
        assert resp.status_code == 200
        assert "trial_period_days" not in fake_stripe.checkout_calls[-1]["subscription_data"]

        fake_stripe.add(make_subscription("sub_2", user_id=subscriber.id))
        webhook(
            "checkout.session.completed",
            {"id": "cs_2", "mode": "subscription", "subscription": "sub_2",
             "client_reference_id": str(subscriber.id),
             "metadata": {"user_id": str(subscriber.id), "plan": "pro"}},
        )
        db_session.refresh(subscriber)
        assert subscriber.plan == "pro"
        assert subscriber.stripe_subscription_id == "sub_2"

    def test_history_is_preserved_across_resubscription(
        self, api_client, subscriber, catalog, fake_stripe, webhook, emails, db_session
    ):
        for sid in ("sub_1", "sub_2"):
            fake_stripe.add(make_subscription(sid, user_id=subscriber.id))
            webhook(
                "checkout.session.completed",
                {"id": f"cs_{sid}", "mode": "subscription", "subscription": sid,
                 "client_reference_id": str(subscriber.id),
                 "metadata": {"user_id": str(subscriber.id), "plan": "pro"}},
            )
            webhook(
                "customer.subscription.deleted",
                {"id": sid, "status": "canceled", "customer": "cus_1",
                 "metadata": {"user_id": str(subscriber.id)}},
            )
        rows = db_session.query(Subscription).filter(
            Subscription.user_id == subscriber.id
        ).all()
        assert {r.stripe_subscription_id for r in rows} == {"sub_1", "sub_2"}
        assert all(r.ended_at is not None for r in rows)

    def test_late_cancellation_of_an_old_subscription_is_ignored(
        self, api_client, subscriber, catalog, fake_stripe, webhook, emails, db_session
    ):
        """Stripe does not guarantee event order.

        A `deleted` for the subscription the customer already replaced must not
        revoke the one they are currently paying for.
        """
        fake_stripe.add(make_subscription("sub_new", user_id=subscriber.id))
        webhook(
            "checkout.session.completed",
            {"id": "cs_new", "mode": "subscription", "subscription": "sub_new",
             "client_reference_id": str(subscriber.id),
             "metadata": {"user_id": str(subscriber.id), "plan": "pro"}},
        )
        db_session.add(
            Subscription(
                user_id=subscriber.id, stripe_subscription_id="sub_old",
                status="active", plan="pro",
            )
        )
        db_session.commit()

        webhook(
            "customer.subscription.deleted",
            {"id": "sub_old", "status": "canceled", "customer": "cus_1",
             "metadata": {"user_id": str(subscriber.id)}},
        )
        db_session.refresh(subscriber)
        assert subscriber.plan == "pro"
        assert subscriber.stripe_subscription_id == "sub_new"

    def test_stale_update_does_not_resurrect_a_dead_subscription(
        self, api_client, subscriber, catalog, fake_stripe, webhook, emails, db_session
    ):
        """A late `updated` for a canceled subscription used to re-attach it.

        `_sync_user_from_subscription` set `user.stripe_subscription_id`
        unconditionally, so the pointer came back pointing at a dead one.
        """
        fake_stripe.add(make_subscription("sub_dead", status="canceled", user_id=subscriber.id))
        subscriber.plan = "free"
        subscriber.stripe_subscription_id = None
        db_session.commit()

        webhook(
            "customer.subscription.updated",
            {"id": "sub_dead", "metadata": {"user_id": str(subscriber.id)}},
        )
        db_session.refresh(subscriber)
        assert subscriber.stripe_subscription_id is None
        assert subscriber.plan == "free"


# --- Webhook robustness -------------------------------------------------------


class TestWebhookRobustness:
    def test_replayed_event_is_a_no_op(
        self, api_client, subscriber, catalog, fake_stripe, webhook, emails, db_session
    ):
        """Stripe retries anything that is not 2xx, and duplicates outright.

        The database writes were idempotent; the emails were not, so a retry
        sent a second welcome for the same subscription.
        """
        fake_stripe.add(make_subscription("sub_1", user_id=subscriber.id))
        session = {
            "id": "cs_1", "mode": "subscription", "subscription": "sub_1",
            "client_reference_id": str(subscriber.id),
            "metadata": {"user_id": str(subscriber.id), "plan": "pro"},
        }
        webhook("checkout.session.completed", session, event_id="evt_same")
        webhook("checkout.session.completed", session, event_id="evt_same")
        assert len(emails["welcome"]) == 1

    def test_unparseable_user_id_does_not_500(
        self, api_client, subscriber, catalog, fake_stripe, webhook, emails
    ):
        """`int(uid)` raised, which returned 500, which made Stripe retry for days."""
        resp = webhook(
            "checkout.session.completed",
            {"id": "cs_x", "mode": "subscription", "subscription": "sub_1",
             "client_reference_id": "not-a-number", "metadata": {}},
        )
        assert resp.status_code == 200

    def test_subscription_without_metadata_is_matched_by_customer(
        self, api_client, subscriber, catalog, fake_stripe, webhook, emails, db_session
    ):
        """Subscriptions created in the Stripe dashboard carry no metadata.

        Those events were silently dropped, so the customer paid and got
        nothing.
        """
        fake_stripe.add(
            make_subscription("sub_dash", plan_metadata=None, customer="cus_1")
        )
        webhook("customer.subscription.updated", {"id": "sub_dash", "customer": "cus_1"})
        db_session.refresh(subscriber)
        assert subscriber.plan == "pro"
        assert subscriber.stripe_subscription_id == "sub_dash"

    def test_trial_will_end_notifies_the_customer(
        self, api_client, subscriber, catalog, fake_stripe, webhook, emails, db_session
    ):
        subscriber.stripe_subscription_id = "sub_1"
        db_session.commit()
        webhook(
            "customer.subscription.trial_will_end",
            {"id": "sub_1", "customer": "cus_1", "trial_end": _ts(NOW + timedelta(days=3)),
             "metadata": {"user_id": str(subscriber.id)}},
        )
        assert len(emails["trial_ending"]) == 1

    def test_payment_failure_notifies_the_customer(
        self, api_client, subscriber, catalog, fake_stripe, webhook, emails, db_session
    ):
        subscriber.stripe_subscription_id = "sub_1"
        db_session.commit()
        webhook(
            "invoice.payment_failed",
            {"id": "in_1", "subscription": "sub_1", "customer": "cus_1",
             "hosted_invoice_url": "https://invoice.stripe.test/in_1"},
        )
        assert len(emails["payment_failed"]) == 1

    def test_handlers_never_call_get_on_a_stripe_object(
        self, api_client, subscriber, catalog, fake_stripe, webhook, emails, db_session
    ):
        """The bug that killed the webhook in production, pinned directly.

        `stripe.Webhook.construct_event` returns an Event whose nested
        `data.object` is a `StripeObject` — not a dict since stripe>=12, and
        with no `.get()`. Every handler branch opened with `session.get("mode")`
        or `sub.get("id")`, so every delivery raised `AttributeError: get`,
        returned 500, and Stripe retried until it disabled the endpoint. Only
        the frontend's `checkout-session-status` polling kept subscriptions in
        sync at all, which is why the breakage stayed invisible.

        Asserting a 200 across every branch is what makes the accessor choice
        non-negotiable; `StripeObj` refuses `.get` exactly like the real thing.
        """
        fake_stripe.add(make_subscription("sub_1", user_id=subscriber.id))
        branches = [
            ("checkout.session.completed", {
                "id": "cs_1", "mode": "subscription", "subscription": "sub_1",
                "client_reference_id": str(subscriber.id),
                "metadata": {"user_id": str(subscriber.id), "plan": "pro"}}),
            ("customer.subscription.created", {
                "id": "sub_1", "metadata": {"user_id": str(subscriber.id)}}),
            ("customer.subscription.updated", {
                "id": "sub_1", "metadata": {"user_id": str(subscriber.id)}}),
            ("customer.subscription.trial_will_end", {
                "id": "sub_1", "customer": "cus_1",
                "trial_end": _ts(NOW + timedelta(days=3)),
                "metadata": {"user_id": str(subscriber.id)}}),
            ("invoice.payment_failed", {
                "id": "in_1", "subscription": "sub_1", "customer": "cus_1",
                "hosted_invoice_url": "https://invoice.stripe.test/in_1"}),
            ("product.created", {
                "id": "prod_x", "name": "Editube", "active": True, "metadata": {}}),
            ("product.deleted", {"id": "prod_x"}),
            ("price.created", {
                "id": "price_x", "product": "prod_editube", "currency": "usd",
                "unit_amount": 4900, "active": True,
                "recurring": {"interval": "month"},
                "metadata": {"editube_plan": "pro", "editube_interval": "month"}}),
            ("price.deleted", {"id": "price_x"}),
            ("customer.subscription.deleted", {
                "id": "sub_1", "status": "canceled", "customer": "cus_1",
                "metadata": {"user_id": str(subscriber.id)}}),
        ]
        for etype, payload in branches:
            resp = webhook(etype, payload)
            assert resp.status_code == 200, f"{etype} returned {resp.status_code}"

    def test_price_webhook_actually_lands_in_the_catalog(
        self, api_client, subscriber, catalog, fake_stripe, webhook, db_session
    ):
        """A 200 is not enough — the nested metadata has to survive conversion.

        `to_dict_recursive` was removed in stripe 15, so the old converter fell
        through to a shallow `to_dict()` and left `metadata` as a StripeObject
        that the parser could not read.
        """
        webhook("price.created", {
            "id": "price_brand_new", "product": "prod_editube", "currency": "usd",
            "unit_amount": 12300, "active": True,
            "recurring": {"interval": "year"},
            "metadata": {"editube_plan": "scale", "editube_interval": "year"},
        })
        row = (
            db_session.query(StripePrice)
            .filter(StripePrice.stripe_price_id == "price_brand_new")
            .one()
        )
        assert (row.editube_plan, row.editube_interval) == ("scale", "year")
        assert row.unit_amount == 12300

    def test_missing_signature_is_rejected(self, api_client, monkeypatch):
        monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
        import app.api.routes.billing as billing

        monkeypatch.setattr(billing.stripe, "api_key", "sk_test_fake")
        resp = api_client.post("/billing/webhook", content=b"{}")
        assert resp.status_code == 400


# --- Direct plan escalation ---------------------------------------------------


class TestPlanEscalation:
    def test_onboarding_cannot_grant_a_paid_tier(
        self, api_client, subscriber, db_session
    ):
        """One PUT bought a 5 TB cap and 200 UGC credits a month, for free.

        `PUT /users/onboarding/plan` wrote `current_user.plan`, which is the
        field every quota reads.
        """
        resp = api_client.login(subscriber).put(
            "/users/onboarding/plan", json={"plan": "scale"}
        )
        assert resp.status_code == 200
        db_session.refresh(subscriber)
        assert subscriber.plan == "free"
        assert subscriber.selected_plan == "scale", "the choice is still remembered"

    def test_onboarding_plan_applies_once_the_subscription_exists(
        self, api_client, subscriber, db_session
    ):
        db_session.add(
            Subscription(
                user_id=subscriber.id, stripe_subscription_id="sub_1",
                status="active", plan="scale",
            )
        )
        db_session.commit()
        api_client.login(subscriber).put("/users/onboarding/plan", json={"plan": "scale"})
        db_session.refresh(subscriber)
        assert subscriber.plan == "scale"

    def test_case_variant_plan_is_not_silently_downgraded(
        self, api_client, subscriber, db_session
    ):
        """`PLAN_ALIASES.get("PRO", "free")` quietly answered "free"."""
        from app.services.pricing import resolve_plan_key

        assert resolve_plan_key("PRO") == "pro"
        assert resolve_plan_key("  Scale ") == "scale"
        assert resolve_plan_key("platinum") is None

    def test_completing_free_onboarding_does_not_burn_the_trial(
        self, api_client, make_user, db_session, catalog, fake_stripe
    ):
        """Signing up Free then upgrading later must still get the 14 days."""
        user = make_user(email="freebie@example.test", plan="free")
        db_session.commit()
        api_client.login(user).post("/users/onboarding/complete-free")
        db_session.refresh(user)

        resp = api_client.post("/billing/checkout", json={"plan": "pro", "interval": "month"})
        assert resp.status_code == 200
        assert fake_stripe.checkout_calls[-1]["subscription_data"]["trial_period_days"] == 14

    def test_free_downgrade_is_refused_while_paying(
        self, api_client, subscriber, db_session
    ):
        db_session.add(
            Subscription(
                user_id=subscriber.id, stripe_subscription_id="sub_1",
                status="active", plan="pro",
            )
        )
        db_session.commit()
        resp = api_client.login(subscriber).post("/users/onboarding/complete-free")
        assert resp.status_code == 409


# --- Quotas follow the tier ---------------------------------------------------


class TestQuotaEnforcement:
    def _workspace(self, db_session, owner, name="Team"):
        ws = Workspace(name=name, slug=f"ws-{owner.id}", owner_user_id=owner.id)
        db_session.add(ws)
        db_session.flush()
        db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=owner.id, role="owner"))
        db_session.commit()
        return ws

    def test_storage_cap_follows_the_workspace_owner_not_the_uploader(
        self, db_session, make_user
    ):
        """A Pro member uploading into someone's Free workspace got a 2 TB cap."""
        from app.services.storage_policy import get_workspace_storage_snapshot
        from app.services.pricing import GB_IN_BYTES

        owner = make_user(email="freeowner@example.test", plan="free")
        db_session.commit()
        ws = self._workspace(db_session, owner)
        uploader = make_user(email="prouser@example.test", plan="pro")
        db_session.commit()

        snap = get_workspace_storage_snapshot(
            db_session, user=uploader, workspace_id=ws.id
        )
        assert snap.cap_bytes == 10 * GB_IN_BYTES

    def test_seat_cap_is_enforced_on_invites(
        self, api_client, db_session, make_user, monkeypatch
    ):
        """`seat_cap` was advertised and metered but never checked."""
        import app.api.routes.workspaces as ws_routes

        monkeypatch.setattr(
            ws_routes, "send_workspace_invite_email", lambda *a, **k: True
        )
        owner = make_user(email="capowner@example.test", plan="free")
        db_session.commit()
        ws = self._workspace(db_session, owner)

        api_client.login(owner)
        first = api_client.post(f"/workspaces/{ws.id}/invites", json={"email": "a@example.test"})
        second = api_client.post(f"/workspaces/{ws.id}/invites", json={"email": "b@example.test"})
        assert first.status_code == 200
        assert second.status_code == 200  # owner + 2 pending == cap of 3

        third = api_client.post(f"/workspaces/{ws.id}/invites", json={"email": "c@example.test"})
        assert third.status_code == 402

    def test_pending_invites_count_against_the_cap(
        self, api_client, db_session, make_user
    ):
        """Otherwise the cap is bypassed by sending them all at once."""
        from app.services.storage_policy import workspace_seat_usage

        owner = make_user(email="pend@example.test", plan="free")
        db_session.commit()
        ws = self._workspace(db_session, owner)
        db_session.add(
            WorkspaceInvite(
                workspace_id=ws.id, email="p@example.test", role="editor",
                token="tok-1", invited_by_user_id=owner.id,
                expires_at=datetime.utcnow() + timedelta(days=14),
            )
        )
        db_session.commit()
        used, cap = workspace_seat_usage(db_session, ws.id)
        assert (used, cap) == (2, 3)

    def test_paid_workspace_has_no_seat_cap(self, db_session, make_user):
        from app.services.storage_policy import workspace_seat_usage

        owner = make_user(email="proowner@example.test", plan="pro")
        db_session.commit()
        ws = self._workspace(db_session, owner)
        _, cap = workspace_seat_usage(db_session, ws.id)
        assert cap is None


# --- Multi-product Stripe accounts --------------------------------------------


class TestCrossProductIsolation:
    """One Stripe account can serve several unrelated products.

    A person who buys two of them has a single `cus_` id for both, so the
    customer-id lookup — the thing that makes dashboard-created subscriptions
    reachable — is also the thing that can attach someone else's purchase to an
    Editube account. A live account was showing Editube `pro` on the strength
    of a subscription to a different product entirely.
    """

    @pytest.fixture
    def foreign_price(self, db_session):  # noqa: ANN001
        """A price we mirror but do not sell: another product on the account."""
        db_session.add(StripeProduct(stripe_product_id="prod_other", name="Some Other App"))
        db_session.add(
            StripePrice(
                stripe_price_id="price_other_pro",
                stripe_product_id="prod_other",
                currency="usd",
                unit_amount=999,
                recurring_interval="month",
                active=True,
                editube_plan=None,      # mirrored, unmapped — the positive signal
                editube_interval="month",
            )
        )
        db_session.commit()

    def test_another_products_subscription_grants_nothing(
        self, api_client, subscriber, catalog, foreign_price, fake_stripe, webhook,
        emails, db_session,
    ):
        fake_stripe.add(
            make_subscription("sub_foreign", price_id="price_other_pro", customer="cus_1")
        )
        resp = webhook(
            "customer.subscription.updated",
            {"id": "sub_foreign", "customer": "cus_1"},
        )
        assert resp.status_code == 200
        db_session.refresh(subscriber)
        assert subscriber.plan == "free"
        assert subscriber.stripe_subscription_id is None

    def test_foreign_plan_metadata_cannot_claim_a_tier(
        self, api_client, subscriber, catalog, foreign_price, fake_stripe, webhook,
        emails, db_session,
    ):
        """Metadata must not rescue a price we have positively identified as
        not ours — otherwise the guard is bypassed by one metadata key."""
        fake_stripe.add(
            make_subscription(
                "sub_foreign", price_id="price_other_pro", customer="cus_1",
                plan_metadata="scale",
            )
        )
        webhook("customer.subscription.updated", {"id": "sub_foreign", "customer": "cus_1"})
        db_session.refresh(subscriber)
        assert subscriber.plan == "free"

    def test_a_foreign_subscription_does_not_revoke_a_real_one(
        self, api_client, subscriber, catalog, foreign_price, fake_stripe, webhook,
        emails, db_session,
    ):
        """The other direction: cancelling the other product must not downgrade
        an Editube customer who is still paying us."""
        fake_stripe.add(make_subscription("sub_1", user_id=subscriber.id))
        webhook("checkout.session.completed", {
            "id": "cs_1", "mode": "subscription", "subscription": "sub_1",
            "client_reference_id": str(subscriber.id),
            "metadata": {"user_id": str(subscriber.id), "plan": "pro"}})
        db_session.refresh(subscriber)
        assert subscriber.plan == "pro"

        fake_stripe.add(
            make_subscription(
                "sub_foreign", status="canceled", price_id="price_other_pro",
                customer="cus_1",
            )
        )
        webhook("customer.subscription.updated", {"id": "sub_foreign", "customer": "cus_1"})
        db_session.refresh(subscriber)
        assert subscriber.plan == "pro"
        assert subscriber.stripe_subscription_id == "sub_1"

    def test_an_unknown_price_still_resolves_via_metadata(
        self, api_client, subscriber, catalog, fake_stripe, webhook, emails, db_session
    ):
        """"Never seen this price" is a stale catalog, not a foreign product.

        Treating the two the same would revoke every customer whenever the
        catalog fell behind.
        """
        fake_stripe.add(
            make_subscription(
                "sub_1", price_id="price_not_synced_at_all", plan_metadata="pro",
                user_id=subscriber.id,
            )
        )
        webhook("customer.subscription.updated",
                {"id": "sub_1", "metadata": {"user_id": str(subscriber.id)}})
        db_session.refresh(subscriber)
        assert subscriber.plan == "pro"


class TestUnpurchasablePlans:
    def test_a_plan_with_no_stripe_price_is_a_409_not_a_500(
        self, api_client, subscriber, db_session, fake_stripe, monkeypatch
    ):
        """Scale is in PLAN_SPECS and the pricing UI but has no Stripe product.

        Checkout raised `500 Missing Stripe price configuration`, which reads
        as an outage rather than "we do not sell this yet".
        """
        monkeypatch.delenv("STRIPE_PRICE_FALLBACK", raising=False)
        resp = api_client.login(subscriber).post(
            "/billing/checkout", json={"plan": "scale", "interval": "month"}
        )
        assert resp.status_code == 409
        assert "not available" in resp.json()["detail"].lower()

    def test_catalog_omits_a_plan_that_cannot_be_bought(
        self, api_client, subscriber, db_session, monkeypatch
    ):
        monkeypatch.delenv("STRIPE_PRICE_FALLBACK", raising=False)
        db_session.add(StripeProduct(stripe_product_id="prod_editube", name="Editube"))
        db_session.add(
            StripePrice(
                stripe_price_id="price_pro_month", stripe_product_id="prod_editube",
                currency="usd", unit_amount=1000, recurring_interval="month",
                active=True, editube_plan="pro", editube_interval="month",
            )
        )
        db_session.commit()

        body = api_client.login(subscriber).get("/billing/catalog").json()
        keys = {p["key"] for p in body["plans"]}
        assert "pro" in keys
        assert "scale" not in keys, "a plan with no price must not be advertised"
        assert "free" in keys and "enterprise" in keys

    def test_catalog_marks_free_and_enterprise_correctly(
        self, api_client, subscriber, catalog, db_session
    ):
        body = api_client.login(subscriber).get("/billing/catalog").json()
        by_key = {p["key"]: p for p in body["plans"]}
        assert by_key["free"]["purchasable"] is True
        assert by_key["enterprise"]["contact_sales"] is True
        assert by_key["pro"]["purchasable"] is True

    def test_product_name_beats_stray_plan_metadata_on_a_foreign_price(
        self, api_client, subscriber, catalog, fake_stripe, webhook, emails, db_session
    ):
        """Defence in depth for the entitlement path itself.

        Even if a foreign price somehow carried an `editube_plan` in the local
        catalog, its product is not an Editube product and it grants nothing.
        """
        db_session.add(StripeProduct(stripe_product_id="prod_foreign", name="Darlin 3 Pro"))
        db_session.add(
            StripePrice(
                stripe_price_id="price_foreign_mapped",
                stripe_product_id="prod_foreign",
                currency="usd", unit_amount=999, recurring_interval="month",
                active=True,
                editube_plan="scale",      # wrong, and must not be honoured
                editube_interval="month",
            )
        )
        db_session.commit()

        fake_stripe.add(
            make_subscription(
                "sub_f", price_id="price_foreign_mapped", customer="cus_1",
                plan_metadata="scale",
            )
        )
        webhook("customer.subscription.updated", {"id": "sub_f", "customer": "cus_1"})
        db_session.refresh(subscriber)
        assert subscriber.plan == "free"


class TestBasicTier:
    """Basic is a real paid tier between Free and Pro, not an alias for Free.

    `PLAN_ALIASES` mapped `basic` -> `free` while no Basic product was sold, so
    mapping the $6/mo Editube Basic price without changing that would have
    charged for the free allowance.
    """

    @pytest.fixture
    def basic_catalog(self, db_session, catalog):  # noqa: ANN001
        db_session.add(
            StripePrice(
                stripe_price_id="price_basic_month",
                stripe_product_id="prod_editube",
                currency="usd", unit_amount=600, recurring_interval="month",
                active=True, editube_plan="basic", editube_interval="month",
            )
        )
        db_session.add(
            StripePrice(
                stripe_price_id="price_basic_year",
                stripe_product_id="prod_editube",
                currency="usd", unit_amount=6000, recurring_interval="year",
                active=True, editube_plan="basic", editube_interval="year",
            )
        )
        db_session.commit()

    def test_basic_resolves_to_itself_not_to_free(self):
        from app.services.pricing import get_plan_spec, resolve_plan_key

        assert resolve_plan_key("basic") == "basic"
        assert get_plan_spec("basic").label == "Basic"

    def test_basic_sits_between_free_and_pro(self):
        from app.services.pricing import PLAN_SPECS

        free, basic, pro = PLAN_SPECS["free"], PLAN_SPECS["basic"], PLAN_SPECS["pro"]
        assert free.included_storage_bytes < basic.included_storage_bytes < pro.included_storage_bytes
        assert free.ugc_credits_monthly < basic.ugc_credits_monthly < pro.ugc_credits_monthly
        assert free.seat_cap < basic.seat_cap
        assert pro.seat_cap is None  # unlimited

    def test_basic_can_be_purchased(
        self, api_client, subscriber, basic_catalog, fake_stripe
    ):
        resp = api_client.login(subscriber).post(
            "/billing/checkout", json={"plan": "basic", "interval": "month"}
        )
        assert resp.status_code == 200
        assert fake_stripe.checkout_calls[-1]["line_items"][0]["price"] == "price_basic_month"

    def test_a_basic_subscription_grants_basic_quotas(
        self, api_client, subscriber, basic_catalog, fake_stripe, webhook, emails, db_session
    ):
        fake_stripe.add(
            make_subscription(
                "sub_b", price_id="price_basic_month", plan_metadata=None,
                user_id=subscriber.id,
            )
        )
        webhook("checkout.session.completed", {
            "id": "cs_b", "mode": "subscription", "subscription": "sub_b",
            "client_reference_id": str(subscriber.id),
            "metadata": {"user_id": str(subscriber.id)}})
        db_session.refresh(subscriber)
        assert subscriber.plan == "basic"
        assert subscriber.onboarding_completed is True

    def test_cancelling_basic_drops_to_free(
        self, api_client, subscriber, basic_catalog, fake_stripe, webhook, emails, db_session
    ):
        fake_stripe.add(
            make_subscription("sub_b", price_id="price_basic_month",
                              plan_metadata=None, user_id=subscriber.id)
        )
        webhook("checkout.session.completed", {
            "id": "cs_b", "mode": "subscription", "subscription": "sub_b",
            "client_reference_id": str(subscriber.id),
            "metadata": {"user_id": str(subscriber.id)}})
        db_session.refresh(subscriber)
        assert subscriber.plan == "basic"

        webhook("customer.subscription.deleted", {
            "id": "sub_b", "status": "canceled", "customer": "cus_1",
            "metadata": {"user_id": str(subscriber.id)}})
        db_session.refresh(subscriber)
        assert subscriber.plan == "free"

    def test_upgrading_basic_to_pro_in_the_portal_takes_effect(
        self, api_client, subscriber, basic_catalog, fake_stripe, webhook, emails, db_session
    ):
        fake_stripe.add(
            make_subscription("sub_b", price_id="price_basic_month",
                              plan_metadata="basic", user_id=subscriber.id)
        )
        webhook("checkout.session.completed", {
            "id": "cs_b", "mode": "subscription", "subscription": "sub_b",
            "client_reference_id": str(subscriber.id),
            "metadata": {"user_id": str(subscriber.id), "plan": "basic"}})
        db_session.refresh(subscriber)
        assert subscriber.plan == "basic"

        # Portal swaps the price; metadata still says basic.
        fake_stripe.subscriptions["sub_b"] = make_subscription(
            "sub_b", price_id="price_pro_month", plan_metadata="basic",
            user_id=subscriber.id,
        )
        webhook("customer.subscription.updated",
                {"id": "sub_b", "metadata": {"user_id": str(subscriber.id)}})
        db_session.refresh(subscriber)
        assert subscriber.plan == "pro"

    def test_basic_appears_in_the_catalog(
        self, api_client, subscriber, basic_catalog, db_session, monkeypatch
    ):
        monkeypatch.delenv("STRIPE_PRICE_FALLBACK", raising=False)
        body = api_client.login(subscriber).get("/billing/catalog").json()
        by_key = {p["key"]: p for p in body["plans"]}
        assert by_key["basic"]["purchasable"] is True
        assert by_key["basic"]["stripe_prices"]["month"]["unit_amount"] == 600
