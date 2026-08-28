"""Postgres-only lock and uniqueness tests for affiliate money paths.

Set AFFILIATE_TEST_DATABASE_URL to a dedicated database whose name contains
"test". The suite creates and drops one random schema; it refuses the normal
DATABASE_URL and never runs against an ambiguously named database.
"""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import (
    AffiliateApplication,
    AffiliateAttribution,
    AffiliateClick,
    AffiliateComplianceProfile,
    AffiliateCommissionEntry,
    AffiliatePartner,
    AffiliatePayout,
    AffiliateProgramTerms,
    AffiliateTermsAcceptance,
    User,
)
from app.services.affiliate_program import (
    PROGRAM_TERMS_CHECKSUM,
    PROGRAM_TERMS_TEXT,
    AffiliateProgramError,
    claim_attribution,
    create_payout_batch,
    utcnow,
)


pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def pg_session_factory():
    database_url = os.getenv("AFFILIATE_TEST_DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("AFFILIATE_TEST_DATABASE_URL is not configured")
    normal_url = os.getenv("DATABASE_URL", "").strip()
    if normal_url and make_url(database_url).render_as_string(hide_password=True) == make_url(
        normal_url
    ).render_as_string(hide_password=True):
        pytest.fail("AFFILIATE_TEST_DATABASE_URL must not be DATABASE_URL")
    database_name = (make_url(database_url).database or "").lower()
    if "test" not in database_name:
        pytest.fail("Affiliate concurrency tests require a database name containing 'test'")

    schema = f"affiliate_test_{uuid.uuid4().hex}"
    administration_engine = create_engine(database_url, isolation_level="AUTOCOMMIT")
    with administration_engine.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    engine = create_engine(
        database_url,
        pool_size=8,
        max_overflow=0,
    ).execution_options(schema_translate_map={None: schema})
    required_table_names = {
        "users",
        "workspaces",
        "workspace_members",
        "referral_codes",
        "referrals",
        "affiliate_program_terms",
        "affiliate_applications",
        "affiliate_partners",
        "affiliate_campaigns",
        "affiliate_terms_acceptances",
        "affiliate_clicks",
        "affiliate_attributions",
        "affiliate_compliance_profiles",
        "affiliate_commission_entries",
        "affiliate_payouts",
        "affiliate_payout_items",
        "affiliate_audit_events",
    }
    public_tables = [
        table
        for table in Base.metadata.sorted_tables
        if table.schema is None and table.name in required_table_names
    ]
    Base.metadata.create_all(engine, tables=public_tables)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    try:
        yield factory
    finally:
        engine.dispose()
        with administration_engine.connect() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        administration_engine.dispose()


def _seed_program(factory):
    db = factory()
    try:
        terms = AffiliateProgramTerms(
            version=f"pg-{uuid.uuid4().hex}",
            status="active",
            commission_rate_bps=3000,
            commission_months=12,
            attribution_window_days=60,
            payout_minimum_minor=5000,
            hold_days=30,
            currency="usd",
            commission_basis="invoice_amount_paid_excluding_tax",
            legal_text=PROGRAM_TERMS_TEXT,
            legal_copy_checksum=PROGRAM_TERMS_CHECKSUM,
            effective_at=utcnow() - timedelta(minutes=1),
        )
        owner = User(email=f"owner-{uuid.uuid4().hex}@example.test", name="Owner", role="user")
        customer = User(
            email=f"customer-{uuid.uuid4().hex}@example.test", name="Customer", role="user"
        )
        finance_one = User(
            email=f"finance-one-{uuid.uuid4().hex}@example.test", name="Finance one", role="admin"
        )
        finance_two = User(
            email=f"finance-two-{uuid.uuid4().hex}@example.test", name="Finance two", role="admin"
        )
        db.add_all([terms, owner, customer, finance_one, finance_two])
        db.flush()
        application = AffiliateApplication(
            user_id=owner.id,
            email=owner.email,
            display_name="Postgres partner",
            country_code="US",
            audience_description="A test-only partner used to validate database locking semantics.",
            promotion_channels=["video"],
            payout_currency="usd",
            status="approved",
            applicant_attested_at=utcnow(),
            reviewed_at=utcnow(),
        )
        db.add(application)
        db.flush()
        partner = AffiliatePartner(
            user_id=owner.id,
            application_id=application.id,
            terms_version_id=terms.id,
            code=f"PG{uuid.uuid4().hex[:14].upper()}",
            status="active",
            payouts_enabled=False,
            risk_status="clear",
            approved_at=utcnow(),
        )
        db.add(partner)
        db.flush()
        db.add(
            AffiliateComplianceProfile(
                partner_id=partner.id,
                tax_residency_country="US",
                tax_form_type="w9",
                tax_form_reference_hash=f"pg-test-tax-{partner.id}",
                tax_verified_at=utcnow(),
                sanctions_status="clear",
                sanctions_checked_at=utcnow(),
                withholding_rate_bps=0,
                review_note="Postgres concurrency test compliance evidence.",
            )
        )
        db.add(
            AffiliateTermsAcceptance(
                partner_id=partner.id,
                terms_version_id=terms.id,
                accepted_by_user_id=owner.id,
            )
        )
        click = AffiliateClick(
            partner_id=partner.id,
            token=f"pg-click-{uuid.uuid4().hex}",
            occurred_at=utcnow(),
            expires_at=utcnow() + timedelta(days=60),
        )
        db.add(click)
        db.commit()
        return {
            "terms_id": terms.id,
            "owner_id": owner.id,
            "customer_id": customer.id,
            "finance_ids": (finance_one.id, finance_two.id),
            "partner_id": partner.id,
            "click_token": click.token,
        }
    finally:
        db.close()


def test_concurrent_claims_create_one_attribution(pg_session_factory) -> None:
    seed = _seed_program(pg_session_factory)
    gate = Barrier(2)

    def claim() -> int:
        db = pg_session_factory()
        try:
            user = db.get(User, seed["customer_id"])
            gate.wait(timeout=10)
            return claim_attribution(
                db,
                user=user,
                click_token=seed["click_token"],
            ).id
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        ids = list(executor.map(lambda _index: claim(), range(2)))

    db = pg_session_factory()
    try:
        assert ids[0] == ids[1]
        assert db.query(AffiliateAttribution).filter(
            AffiliateAttribution.invitee_user_id == seed["customer_id"]
        ).count() == 1
    finally:
        db.close()


def test_concurrent_payout_drafts_assign_each_entry_once(
    pg_session_factory,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AFFILIATE_SUPPORTED_COUNTRIES", "US")
    seed = _seed_program(pg_session_factory)
    setup = pg_session_factory()
    try:
        attribution = AffiliateAttribution(
            partner_id=seed["partner_id"],
            click_id=(
                setup.query(AffiliateClick.id)
                .filter(AffiliateClick.token == seed["click_token"])
                .scalar()
            ),
            invitee_user_id=seed["customer_id"],
            terms_version_id=seed["terms_id"],
            status="active",
            attributed_at=utcnow(),
        )
        setup.add(attribution)
        setup.flush()
        setup.add(
            AffiliateCommissionEntry(
                partner_id=seed["partner_id"],
                attribution_id=attribution.id,
                terms_version_id=seed["terms_id"],
                source_key=f"pg-payout-{uuid.uuid4().hex}",
                entry_type="manual_adjustment",
                amount_minor=6000,
                commissionable_minor=6000,
                rate_bps=10_000,
                currency="usd",
                available_at=utcnow() - timedelta(minutes=1),
                description="Postgres payout lock test",
            )
        )
        setup.commit()
    finally:
        setup.close()

    gate = Barrier(2)

    def draft(admin_id: int) -> str:
        db = pg_session_factory()
        try:
            partner = db.get(AffiliatePartner, seed["partner_id"])
            admin = db.get(User, admin_id)
            gate.wait(timeout=10)
            try:
                payout = create_payout_batch(db, partner=partner, admin=admin)
                return f"created:{payout.id}"
            except AffiliateProgramError as exc:
                db.rollback()
                return f"rejected:{exc.reason}"
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(draft, seed["finance_ids"]))

    assert sum(result.startswith("created:") for result in results) == 1
    assert results.count("rejected:below_threshold") == 1
    db = pg_session_factory()
    try:
        assert db.query(AffiliatePayout).count() == 1
    finally:
        db.close()
