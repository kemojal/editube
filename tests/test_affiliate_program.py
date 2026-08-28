from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from app.db.models import (
    AccountCreditLedger,
    AffiliateApplication,
    AffiliateAttribution,
    AffiliateCampaign,
    AffiliateClick,
    AffiliateComplianceProfile,
    AffiliateCommissionEntry,
    AffiliateCommissionState,
    AffiliatePartner,
    AffiliatePayout,
    AffiliateProgramTerms,
    AffiliateLaunchApproval,
    AffiliatePayoutItem,
    AffiliateTermsAcceptance,
    AffiliateAuditEvent,
    Referral,
    ReferralAdminAuditEvent,
    ReferralCode,
    ReferralEmailEvent,
    ReferralEmailSuppression,
    ReferralInviteDelivery,
    User,
    UserMFAMethod,
)
from app.services.affiliate_program import (
    PROGRAM_TERMS_CHECKSUM,
    PROGRAM_TERMS_TEXT,
    AffiliateProgramError,
    accept_partner_terms,
    approve_payout,
    build_partner_link,
    claim_attribution,
    create_partner_campaign,
    create_payout_batch,
    launch_approval_state,
    record_click,
    record_dispute_opened,
    record_dispute_won,
    record_manual_adjustment,
    record_paid_invoice,
    record_refund,
    partner_dashboard,
    record_launch_approval,
    review_application,
    submit_application,
    terms_launch_ready,
    update_partner_campaign,
    utcnow,
    withdraw_application,
)


@pytest.fixture(autouse=True)
def _affiliate_test_country_allowlist(monkeypatch):
    monkeypatch.setenv("AFFILIATE_SUPPORTED_COUNTRIES", "US")


def _user(db, email: str, *, role: str = "user") -> User:
    user = User(email=email, name=email.split("@")[0], role=role, onboarding_completed=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _terms(db) -> AffiliateProgramTerms:
    terms = AffiliateProgramTerms(
        version=f"v-{utcnow().timestamp()}",
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
    db.add(terms)
    db.flush()
    approvers = []
    for role in ("legal", "finance", "product", "engineering"):
        approver = User(
            email=f"affiliate-{role}-{terms.id}@example.invalid",
            name=f"{role.title()} approver",
            role="admin",
            onboarding_completed=False,
        )
        db.add(approver)
        approvers.append((role, approver))
    db.flush()
    db.add_all(
        [
            AffiliateLaunchApproval(
                terms_version_id=terms.id,
                approval_role=role,
                approved_by_user_id=approver.id,
                terms_checksum=terms.legal_copy_checksum,
                note=f"Test evidence confirms the {role} launch gate is approved.",
            )
            for role, approver in approvers
        ]
    )
    db.commit()
    db.refresh(terms)
    return terms


def _partner(db, terms, owner: User, *, status: str = "active") -> AffiliatePartner:
    application = AffiliateApplication(
        user_id=owner.id,
        email=owner.email,
        display_name=owner.name,
        country_code="US",
        audience_description="A real audience description long enough for the program application review workflow.",
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
        code=f"EDT{owner.id:010d}",
        status=status,
        approved_at=utcnow(),
        risk_status="clear",
        payouts_enabled=False,
    )
    db.add(partner)
    db.flush()
    db.add(
        AffiliateComplianceProfile(
            partner_id=partner.id,
            tax_residency_country="US",
            tax_form_type="w9",
            tax_form_reference_hash=f"test-tax-evidence-{partner.id}",
            tax_verified_at=utcnow(),
            sanctions_status="clear",
            sanctions_checked_at=utcnow(),
            withholding_rate_bps=0,
            review_note="Verified tax evidence and clear sanctions screening for this test partner.",
        )
    )
    db.commit()
    db.refresh(partner)
    return partner


def _attribution(db, terms, partner, customer: User) -> AffiliateAttribution:
    click = AffiliateClick(
        partner_id=partner.id,
        token=f"token-{partner.id}-{customer.id}-abcdefghijklmnopqrstuvwxyz",
        occurred_at=utcnow(),
        expires_at=utcnow() + timedelta(days=60),
    )
    db.add(click)
    db.flush()
    attribution = AffiliateAttribution(
        partner_id=partner.id,
        click_id=click.id,
        invitee_user_id=customer.id,
        terms_version_id=terms.id,
        status="active",
        attributed_at=utcnow(),
    )
    db.add(attribution)
    db.commit()
    db.refresh(attribution)
    return attribution


def _invoice(invoice_id: str, *, paid: int = 1000, excluding_tax: int = 900, created: int = 1_788_153_600):
    return {
        "id": invoice_id,
        "amount_paid": paid,
        "total": paid,
        "total_excluding_tax": excluding_tax,
        "lines": {
            "has_more": False,
            "data": [
                {
                    "type": "subscription",
                    "amount": excluding_tax,
                    "amount_excluding_tax": excluding_tax,
                }
            ],
        },
        "currency": "usd",
        "created": created,
        "status_transitions": {"paid_at": created},
        "charge": f"ch_{invoice_id}",
    }


def test_partner_link_encodes_campaign_parameters(monkeypatch) -> None:
    monkeypatch.setenv("FRONTEND_BASE_URL", "https://app.example.com/")
    assert build_partner_link("PARTNER123", "launch & learn") == (
        "https://app.example.com/signup?aff=PARTNER123&campaign=launch+%26+learn"
    )


def test_click_velocity_is_flagged_then_rate_limited(db_session, monkeypatch) -> None:
    monkeypatch.setenv("AFFILIATE_LEGAL_APPROVED", "1")
    monkeypatch.setenv("AFFILIATE_HASH_SECRET", "test-only-affiliate-hash-secret")
    terms = _terms(db_session)
    owner = _user(db_session, "velocity-owner@example.com")
    partner = _partner(db_session, terms, owner)
    for index in range(20):
        click = record_click(
            db_session,
            code=partner.code,
            campaign=None,
            landing_path="/signup",
            referrer="https://partner.example.com/guide",
            ip="203.0.113.25",
            user_agent="TestBrowser/1",
        )
        assert click.risk_flags is None

    flagged = record_click(
        db_session,
        code=partner.code,
        campaign=None,
        landing_path="/signup",
        referrer="https://partner.example.com/guide",
        ip="203.0.113.25",
        user_agent="TestBrowser/1",
    )
    assert flagged.risk_flags == ["ip_velocity"]
    assert flagged.ip_hash != "203.0.113.25"
    assert flagged.user_agent_hash != "TestBrowser/1"

    for index in range(79):
        record_click(
            db_session,
            code=partner.code,
            campaign=None,
            landing_path="/signup",
            referrer=None,
            ip="203.0.113.25",
            user_agent="TestBrowser/1",
        )
    with pytest.raises(AffiliateProgramError, match="too many requests"):
        record_click(
            db_session,
            code=partner.code,
            campaign=None,
            landing_path="/signup",
            referrer=None,
            ip="203.0.113.25",
            user_agent="TestBrowser/1",
        )
    assert db_session.query(AffiliateClick).filter(
        AffiliateClick.partner_id == partner.id
    ).count() == 100


def _acceptance(db, partner: AffiliatePartner, owner: User) -> AffiliateTermsAcceptance:
    acceptance = AffiliateTermsAcceptance(
        partner_id=partner.id,
        terms_version_id=partner.terms_version_id,
        accepted_by_user_id=owner.id,
        ip_hash="test-ip-hash",
        user_agent_hash="test-ua-hash",
    )
    db.add(acceptance)
    db.commit()
    return acceptance


def test_applications_fail_closed_until_legal_terms_are_live(db_session, monkeypatch) -> None:
    monkeypatch.delenv("AFFILIATE_LEGAL_APPROVED", raising=False)
    _terms(db_session)
    applicant = _user(db_session, "applicant@example.com")

    with pytest.raises(AffiliateProgramError, match="not open"):
        submit_application(
            db_session,
            user=applicant,
            display_name="Applicant",
            business_name=None,
            website_url="https://example.com",
            country_code="US",
            audience_description="I teach post-production teams through detailed tutorials and a private community.",
            audience_size=5000,
            promotion_channels=["video"],
            attested=True,
        )


def test_migration_seed_preserves_the_exact_deployed_legal_copy() -> None:
    from pathlib import Path
    import runpy

    migration = runpy.run_path(
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "af2708270001_affiliate_program.py"
    )
    assert migration["V1_LEGAL_TEXT"] == PROGRAM_TERMS_TEXT
    assert migration["V1_LEGAL_CHECKSUM"] == PROGRAM_TERMS_CHECKSUM


def test_application_review_terms_acceptance_and_first_touch_claim(db_session, monkeypatch) -> None:
    monkeypatch.setenv("AFFILIATE_LEGAL_APPROVED", "1")
    monkeypatch.setenv("AFFILIATE_SUPPORTED_COUNTRIES", "US")
    terms = _terms(db_session)
    applicant = _user(db_session, "partner@example.com")
    admin = _user(db_session, "reviewer@example.com", role="admin")
    customer = _user(db_session, "customer@example.com")

    application = submit_application(
        db_session,
        user=applicant,
        display_name="Partner Studio",
        business_name="Partner Studio LLC",
        website_url="https://partner.example.com/resources",
        country_code="US",
        audience_description="I publish detailed editing workflows for working post-production teams and agencies.",
        audience_size=12_500,
        promotion_channels=["video", "newsletter", "video"],
        attested=True,
    )
    application, partner = review_application(
        db_session,
        application=application,
        admin=admin,
        decision="approved",
        notes="Audience and promotion methods verified.",
    )
    assert application.status == "approved"
    assert partner is not None and partner.status == "pending_terms"
    assert partner.risk_status == "review"
    assert partner.hold_reason == "Compliance review pending."

    acceptance = accept_partner_terms(
        db_session,
        partner=partner,
        user=applicant,
        version=terms.version,
        checksum=PROGRAM_TERMS_CHECKSUM,
        ip="203.0.113.4",
        user_agent="TestBrowser/1",
    )
    db_session.refresh(partner)
    assert acceptance.ip_hash != "203.0.113.4"
    assert partner.status == "active"

    first = AffiliateClick(
        partner_id=partner.id,
        token="first-touch-token-abcdefghijklmnopqrstuvwxyz",
        occurred_at=utcnow(),
        expires_at=utcnow() + timedelta(days=60),
    )
    db_session.add(first)
    db_session.commit()
    attributed = claim_attribution(db_session, user=customer, click_token=first.token)
    assert attributed.partner_id == partner.id
    assert claim_attribution(db_session, user=customer, click_token=first.token).id == attributed.id


def test_pending_application_can_be_withdrawn_and_resubmitted(db_session, monkeypatch) -> None:
    monkeypatch.setenv("AFFILIATE_LEGAL_APPROVED", "1")
    monkeypatch.setenv("AFFILIATE_SUPPORTED_COUNTRIES", "US")
    _terms(db_session)
    applicant = _user(db_session, "withdraw-applicant@example.com")
    payload = {
        "user": applicant,
        "display_name": "Withdraw Applicant",
        "business_name": None,
        "website_url": None,
        "country_code": "US",
        "audience_description": "I publish production tutorials for a specific audience of professional video editors.",
        "audience_size": 1200,
        "promotion_channels": ["video"],
        "attested": True,
    }
    application = submit_application(db_session, **payload)
    withdrawn = withdraw_application(
        db_session, application_id=application.id, user=applicant
    )
    assert withdrawn.status == "withdrawn"
    replacement = submit_application(db_session, **payload)
    assert replacement.id != application.id
    assert replacement.status == "pending"


def test_self_referral_and_expired_click_are_rejected(db_session) -> None:
    terms = _terms(db_session)
    owner = _user(db_session, "owner@example.com")
    customer = _user(db_session, "late@example.com")
    partner = _partner(db_session, terms, owner)
    self_click = AffiliateClick(
        partner_id=partner.id,
        token="self-token-abcdefghijklmnopqrstuvwxyz",
        occurred_at=utcnow(),
        expires_at=utcnow() + timedelta(days=1),
    )
    expired = AffiliateClick(
        partner_id=partner.id,
        token="expired-token-abcdefghijklmnopqrstuvwxyz",
        occurred_at=utcnow() - timedelta(days=61),
        expires_at=utcnow() - timedelta(days=1),
    )
    db_session.add_all([self_click, expired])
    db_session.commit()

    with pytest.raises(AffiliateProgramError, match="own account"):
        claim_attribution(db_session, user=owner, click_token=self_click.token)
    with pytest.raises(AffiliateProgramError, match="expired"):
        claim_attribution(db_session, user=customer, click_token=expired.token)


def test_paid_invoice_uses_collected_revenue_excluding_tax_and_is_idempotent(db_session) -> None:
    terms = _terms(db_session)
    owner = _user(db_session, "owner2@example.com")
    customer = _user(db_session, "buyer@example.com")
    partner = _partner(db_session, terms, owner)
    attribution = _attribution(db_session, terms, partner, customer)

    invoice = _invoice("in_first", paid=1000, excluding_tax=900)
    entry = record_paid_invoice(db_session, user=customer, invoice=invoice, stripe_event_id="evt_1")
    duplicate = record_paid_invoice(db_session, user=customer, invoice=invoice, stripe_event_id="evt_2")

    assert entry is not None and duplicate is not None
    assert entry.id == duplicate.id
    assert entry.commissionable_minor == 900
    assert entry.amount_minor == 270
    db_session.refresh(attribution)
    assert attribution.first_paid_at is not None
    assert attribution.commission_ends_at is not None
    assert entry.available_at > attribution.first_paid_at + timedelta(days=29)


def test_paid_invoice_excludes_one_time_lines_and_allocates_cash(db_session) -> None:
    terms = _terms(db_session)
    owner = _user(db_session, "mixed-invoice-owner@example.com")
    customer = _user(db_session, "mixed-invoice-customer@example.com")
    partner = _partner(db_session, terms, owner)
    _attribution(db_session, terms, partner, customer)
    invoice = _invoice("in_mixed", paid=1200, excluding_tax=1500)
    invoice["total"] = 1800
    invoice["lines"] = {
        "has_more": False,
        "data": [
            {"type": "subscription", "amount_excluding_tax": 1000},
            {"type": "invoiceitem", "amount_excluding_tax": 500},
        ],
    }
    entry = record_paid_invoice(
        db_session,
        user=customer,
        invoice=invoice,
        stripe_event_id="evt_mixed_paid",
    )
    assert entry is not None
    assert entry.commissionable_minor == 667
    assert entry.amount_minor == 200


def test_refunds_are_proportional_cumulative_and_never_over_reverse(db_session) -> None:
    terms = _terms(db_session)
    owner = _user(db_session, "owner3@example.com")
    customer = _user(db_session, "refund@example.com")
    partner = _partner(db_session, terms, owner)
    _attribution(db_session, terms, partner, customer)
    accrual = record_paid_invoice(
        db_session,
        user=customer,
        invoice=_invoice("in_refund", paid=1000, excluding_tax=1000),
        stripe_event_id="evt_paid",
    )
    first = record_refund(
        db_session,
        invoice_id="in_refund",
        charge_id="ch_refund",
        amount_refunded_minor=250,
        invoice_amount_paid_minor=1000,
        stripe_event_id="evt_refund_1",
    )
    second = record_refund(
        db_session,
        invoice_id="in_refund",
        charge_id="ch_refund",
        amount_refunded_minor=1000,
        invoice_amount_paid_minor=1000,
        stripe_event_id="evt_refund_2",
    )
    duplicate = record_refund(
        db_session,
        invoice_id="in_refund",
        charge_id="ch_refund",
        amount_refunded_minor=1000,
        invoice_amount_paid_minor=1000,
        stripe_event_id="evt_refund_2",
    )
    assert accrual is not None and first is not None and second is not None
    assert first.amount_minor == -75
    assert second.amount_minor == -225
    assert duplicate.id == second.id
    total = sum(
        entry.amount_minor
        for entry in db_session.query(AffiliateCommissionEntry)
        .filter(AffiliateCommissionEntry.stripe_invoice_id == "in_refund")
        .all()
    )
    assert total == 0


def test_dispute_reverses_remaining_commission_and_win_reinstates_it(db_session) -> None:
    terms = _terms(db_session)
    owner = _user(db_session, "owner4@example.com")
    customer = _user(db_session, "dispute@example.com")
    partner = _partner(db_session, terms, owner)
    _attribution(db_session, terms, partner, customer)
    record_paid_invoice(
        db_session,
        user=customer,
        invoice=_invoice("in_dispute", paid=2000, excluding_tax=2000),
        stripe_event_id="evt_paid_dispute",
    )
    reversed_entry = record_dispute_opened(
        db_session,
        invoice_id="in_dispute",
        charge_id="ch_dispute",
        stripe_event_id="evt_dispute_open",
    )
    won_entry = record_dispute_won(
        db_session,
        invoice_id="in_dispute",
        charge_id="ch_dispute",
        stripe_event_id="evt_dispute_won",
    )
    assert reversed_entry is not None and reversed_entry.amount_minor == -600
    assert won_entry is not None and won_entry.amount_minor == 600


def test_refund_after_open_dispute_does_not_double_reverse(db_session) -> None:
    terms = _terms(db_session)
    owner = _user(db_session, "owner-dispute-refund@example.com")
    customer = _user(db_session, "dispute-refund@example.com")
    partner = _partner(db_session, terms, owner)
    _attribution(db_session, terms, partner, customer)
    record_paid_invoice(
        db_session,
        user=customer,
        invoice=_invoice("in_dispute_refund", paid=1000, excluding_tax=1000),
        stripe_event_id="evt_dispute_refund_paid",
    )
    record_dispute_opened(
        db_session,
        invoice_id="in_dispute_refund",
        charge_id="ch_dispute_refund",
        stripe_event_id="evt_dispute_refund_open",
    )
    assert record_refund(
        db_session,
        invoice_id="in_dispute_refund",
        charge_id="ch_dispute_refund",
        amount_refunded_minor=1000,
        invoice_amount_paid_minor=1000,
        stripe_event_id="evt_dispute_refund_full",
    ) is None
    total = (
        db_session.query(AffiliateCommissionEntry)
        .filter(AffiliateCommissionEntry.stripe_invoice_id == "in_dispute_refund")
        .all()
    )
    assert sum(entry.amount_minor for entry in total) == 0

    # A won dispute must not restore commission that a full refund made
    # permanently ineligible while the dispute hold was active.
    assert record_dispute_won(
        db_session,
        invoice_id="in_dispute_refund",
        charge_id="ch_dispute_refund",
        stripe_event_id="evt_dispute_refund_won",
    ) is None
    assert sum(
        entry.amount_minor
        for entry in db_session.query(AffiliateCommissionEntry)
        .filter(AffiliateCommissionEntry.stripe_invoice_id == "in_dispute_refund")
        .all()
    ) == 0


def test_partial_refund_dispute_and_win_converge_to_refunded_balance(db_session) -> None:
    terms = _terms(db_session)
    owner = _user(db_session, "owner-partial-dispute@example.com")
    customer = _user(db_session, "partial-dispute@example.com")
    partner = _partner(db_session, terms, owner)
    _attribution(db_session, terms, partner, customer)
    record_paid_invoice(
        db_session,
        user=customer,
        invoice=_invoice("in_partial_dispute", paid=1000, excluding_tax=1000),
        stripe_event_id="evt_partial_dispute_paid",
    )
    refund = record_refund(
        db_session,
        invoice_id="in_partial_dispute",
        charge_id="ch_partial_dispute",
        amount_refunded_minor=250,
        invoice_amount_paid_minor=1000,
        stripe_event_id="evt_partial_dispute_refund",
    )
    reversal = record_dispute_opened(
        db_session,
        invoice_id="in_partial_dispute",
        charge_id="ch_partial_dispute",
        stripe_event_id="evt_partial_dispute_open",
    )
    reinstatement = record_dispute_won(
        db_session,
        invoice_id="in_partial_dispute",
        charge_id="ch_partial_dispute",
        stripe_event_id="evt_partial_dispute_won",
    )

    assert refund is not None and refund.amount_minor == -75
    assert refund.commissionable_minor == -250
    assert reversal is not None and reversal.amount_minor == -225
    assert reversal.commissionable_minor == -750
    assert reinstatement is not None and reinstatement.amount_minor == 225
    assert reinstatement.commissionable_minor == 750
    assert sum(
        entry.amount_minor
        for entry in db_session.query(AffiliateCommissionEntry)
        .filter(AffiliateCommissionEntry.stripe_invoice_id == "in_partial_dispute")
        .all()
    ) == 225


def test_manual_adjustment_is_scoped_audited_and_idempotent(db_session) -> None:
    terms = _terms(db_session)
    owner = _user(db_session, "owner-adjustment@example.com")
    customer = _user(db_session, "adjustment-customer@example.com")
    other_customer = _user(db_session, "adjustment-other@example.com")
    admin = _user(db_session, "adjustment-admin@example.com", role="admin")
    partner = _partner(db_session, terms, owner)
    attribution = _attribution(db_session, terms, partner, customer)

    first = record_manual_adjustment(
        db_session,
        partner=partner,
        attribution_id=attribution.id,
        amount_minor=-125,
        reason="Correct a duplicated legacy commission after finance reconciliation.",
        idempotency_key="finance-ticket-12345",
        admin=admin,
    )
    duplicate = record_manual_adjustment(
        db_session,
        partner=partner,
        attribution_id=attribution.id,
        amount_minor=-125,
        reason="Correct a duplicated legacy commission after finance reconciliation.",
        idempotency_key="finance-ticket-12345",
        admin=admin,
    )
    assert duplicate.id == first.id

    other_owner = _user(db_session, "other-owner-adjustment@example.com")
    other_partner = _partner(db_session, terms, other_owner)
    other_attribution = _attribution(db_session, terms, other_partner, other_customer)
    with pytest.raises(AffiliateProgramError, match="does not belong"):
        record_manual_adjustment(
            db_session,
            partner=partner,
            attribution_id=other_attribution.id,
            amount_minor=100,
            reason="Attempt to apply a correction to the wrong partner attribution.",
            idempotency_key="finance-ticket-wrong-partner",
            admin=admin,
        )


def test_payout_requires_threshold_clear_risk_and_two_admins(db_session) -> None:
    terms = _terms(db_session)
    owner = _user(db_session, "owner5@example.com")
    customer = _user(db_session, "payout@example.com")
    creator = _user(db_session, "finance1@example.com", role="admin")
    approver = _user(db_session, "finance2@example.com", role="admin")
    partner = _partner(db_session, terms, owner)
    _attribution(db_session, terms, partner, customer)
    entry = record_paid_invoice(
        db_session,
        user=customer,
        invoice=_invoice("in_payout", paid=20_000, excluding_tax=20_000),
        stripe_event_id="evt_payout",
    )
    assert entry is not None
    entry.available_at = utcnow() - timedelta(minutes=1)
    db_session.commit()

    payout = create_payout_batch(db_session, partner=partner, admin=creator)
    assert payout.amount_minor == 6000
    with pytest.raises(AffiliateProgramError, match="different administrator"):
        approve_payout(db_session, payout, creator)
    approved = approve_payout(db_session, payout, approver)
    assert approved.status == "approved"


def test_failed_transfer_can_retry_with_the_same_idempotent_batch(db_session, monkeypatch) -> None:
    import stripe
    from app.services.affiliate_program import execute_payout

    monkeypatch.setenv("AFFILIATE_PAYOUTS_ENABLED", "1")
    monkeypatch.setenv("AFFILIATE_LEGAL_APPROVED", "1")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_affiliate")
    terms = _terms(db_session)
    owner = _user(db_session, "retry-owner@example.com")
    customer = _user(db_session, "retry-customer@example.com")
    creator = _user(db_session, "retry-creator@example.com", role="admin")
    approver = _user(db_session, "retry-approver@example.com", role="admin")
    partner = _partner(db_session, terms, owner)
    partner.stripe_connect_account_id = "acct_retry_partner"
    partner.payouts_enabled = True
    _attribution(db_session, terms, partner, customer)
    entry = record_paid_invoice(
        db_session,
        user=customer,
        invoice=_invoice("in_retry", paid=20_000, excluding_tax=20_000),
        stripe_event_id="evt_retry_paid",
    )
    assert entry is not None
    entry.available_at = utcnow() - timedelta(minutes=1)
    db_session.commit()
    payout = approve_payout(
        db_session,
        create_payout_batch(db_session, partner=partner, admin=creator),
        approver,
    )

    monkeypatch.setattr(
        stripe.Account,
        "retrieve",
        lambda *_args, **_kwargs: {"payouts_enabled": True},
    )
    calls: list[str] = []

    def create_transfer(*_args, **kwargs):
        calls.append(kwargs["idempotency_key"])
        if len(calls) == 1:
            raise RuntimeError("temporary Stripe failure")
        return {"id": "tr_retry_succeeded"}

    monkeypatch.setattr(stripe.Transfer, "create", create_transfer)
    with pytest.raises(AffiliateProgramError, match="could not create"):
        execute_payout(db_session, payout, approver)
    db_session.refresh(payout)
    assert payout.status == "failed"
    paid = execute_payout(db_session, payout, approver)
    assert paid.status == "paid"
    assert paid.stripe_transfer_id == "tr_retry_succeeded"
    assert calls == [f"affiliate-payout-{payout.id}"] * 2


def test_refund_after_payout_draft_cancels_stale_batch(db_session) -> None:
    terms = _terms(db_session)
    owner = _user(db_session, "owner6@example.com")
    customer = _user(db_session, "late-refund@example.com")
    creator = _user(db_session, "finance3@example.com", role="admin")
    approver = _user(db_session, "finance4@example.com", role="admin")
    partner = _partner(db_session, terms, owner)
    _attribution(db_session, terms, partner, customer)
    entry = record_paid_invoice(
        db_session,
        user=customer,
        invoice=_invoice("in_late_refund", paid=20_000, excluding_tax=20_000),
        stripe_event_id="evt_late_paid",
    )
    assert entry is not None
    entry.available_at = utcnow() - timedelta(minutes=1)
    db_session.commit()
    payout = create_payout_batch(db_session, partner=partner, admin=creator)
    record_refund(
        db_session,
        invoice_id="in_late_refund",
        charge_id="ch_late_refund",
        amount_refunded_minor=20_000,
        invoice_amount_paid_minor=20_000,
        stripe_event_id="evt_late_refund",
    )

    with pytest.raises(AffiliateProgramError, match="batch was canceled"):
        approve_payout(db_session, payout, approver)
    db_session.refresh(payout)
    assert payout.status == "canceled"
    assert db_session.query(AffiliatePayoutItem).filter(AffiliatePayoutItem.payout_id == payout.id).count() == 0


def test_referral_rewards_wait_for_paid_invoice_and_reverse_precisely(db_session) -> None:
    from app.services.referrals import (
        REFERRER_REWARD_CREDITS,
        reverse_referral_reward_for_invoice,
        reward_referral_from_paid_invoice,
        sync_referral_from_subscription,
    )

    referrer = _user(db_session, "friend-referrer@example.com")
    invitee = _user(db_session, "friend-invitee@example.com")
    code = ReferralCode(user_id=referrer.id, code="FRIEND234", passes_total=3)
    db_session.add(code)
    db_session.flush()
    referral = Referral(
        referrer_user_id=referrer.id,
        referral_code_id=code.id,
        code=code.code,
        invitee_user_id=invitee.id,
        invitee_email=invitee.email,
        status="signed_up",
        signed_up_at=utcnow(),
        pass_trial_days=30,
    )
    db_session.add(referral)
    db_session.commit()

    sync_referral_from_subscription(db_session, invitee, "active", "pro")
    assert db_session.query(AccountCreditLedger).filter(AccountCreditLedger.user_id == referrer.id).count() == 0
    reward_referral_from_paid_invoice(db_session, invitee, "in_referral_first")
    db_session.refresh(referral)
    assert referral.status == "rewarded"
    assert referral.reward_source_invoice_id == "in_referral_first"
    assert sum(item.delta for item in db_session.query(AccountCreditLedger).filter(AccountCreditLedger.user_id == referrer.id)) == REFERRER_REWARD_CREDITS

    reverse_referral_reward_for_invoice(db_session, "in_referral_first")
    db_session.refresh(referral)
    assert referral.status == "void"
    assert referral.reward_reversed_at is not None
    assert sum(item.delta for item in db_session.query(AccountCreditLedger).filter(AccountCreditLedger.user_id == referrer.id)) == 0


def test_affiliate_and_friend_referral_claims_cannot_stack(db_session) -> None:
    from app.services.referrals import ReferralRedemptionError, redeem_referral_code

    terms = _terms(db_session)
    affiliate_owner = _user(db_session, "stack-affiliate-owner@example.com")
    friend_referrer = _user(db_session, "stack-friend-owner@example.com")
    guest_pass_user = _user(db_session, "stack-guest-pass@example.com")
    affiliate_user = _user(db_session, "stack-affiliate-user@example.com")
    partner = _partner(db_session, terms, affiliate_owner)
    code = ReferralCode(user_id=friend_referrer.id, code="STACK234", passes_total=3)
    db_session.add(code)
    click_for_guest = AffiliateClick(
        partner_id=partner.id,
        token="stack-guest-affiliate-token-abcdefghijklmnopqrstuvwxyz",
        occurred_at=utcnow(),
        expires_at=utcnow() + timedelta(days=60),
    )
    click_for_affiliate = AffiliateClick(
        partner_id=partner.id,
        token="stack-affiliate-first-token-abcdefghijklmnopqrstuvwxyz",
        occurred_at=utcnow(),
        expires_at=utcnow() + timedelta(days=60),
    )
    db_session.add_all([click_for_guest, click_for_affiliate])
    db_session.commit()

    redeem_referral_code(db_session, guest_pass_user, code.code)
    with pytest.raises(AffiliateProgramError, match="guest pass"):
        claim_attribution(
            db_session,
            user=guest_pass_user,
            click_token=click_for_guest.token,
        )

    claim_attribution(
        db_session,
        user=affiliate_user,
        click_token=click_for_affiliate.token,
    )
    with pytest.raises(ReferralRedemptionError, match="affiliate"):
        redeem_referral_code(db_session, affiliate_user, code.code)


def test_invite_delivery_attempts_are_recorded_and_resends_are_capped(db_session) -> None:
    from app.services.referrals import ReferralInviteError, resend_email_invite, send_email_invite

    sender = _user(db_session, "invite-sender@example.com")
    invite = send_email_invite(db_session, sender, "new-friend@example.com")
    deliveries = db_session.query(ReferralInviteDelivery).filter(ReferralInviteDelivery.referral_id == invite.id).all()
    assert len(deliveries) == 1
    assert deliveries[0].status == "failed"  # real email is blocked by the test safety fixture

    resend_email_invite(db_session, sender, invite.id)
    deliveries = db_session.query(ReferralInviteDelivery).filter(ReferralInviteDelivery.referral_id == invite.id).all()
    assert [item.attempt_number for item in deliveries] == [1, 2]
    with pytest.raises(ReferralInviteError, match="already sent a reminder"):
        resend_email_invite(db_session, sender, invite.id)


def test_public_registration_cannot_self_assign_admin(api_client, db_session) -> None:
    response = api_client.logout().post(
        "/users/register",
        json={
            "name": "Mallory",
            "email": "mallory-role@example.com",
            "password": "correct horse battery staple",
            "role": "admin",
        },
    )
    assert response.status_code == 200, response.text
    user = db_session.query(User).filter(User.email == "mallory-role@example.com").one()
    assert user.role == "user"

    update = api_client.login(user).put(
        f"/users/{user.id}",
        json={
            "name": "Mallory",
            "email": user.email,
            "password": None,
            "role": "admin",
        },
    )
    assert update.status_code == 200, update.text
    db_session.refresh(user)
    assert user.role == "user"


def test_non_admin_cannot_reach_affiliate_operations(api_client, db_session) -> None:
    user = _user(db_session, "ordinary@example.com")
    response = api_client.login(user).get("/admin/affiliates/applications")
    assert response.status_code == 403


def test_paid_invoice_before_attribution_is_never_commissioned(db_session) -> None:
    terms = _terms(db_session)
    owner = _user(db_session, "historical-owner@example.com")
    customer = _user(db_session, "historical-customer@example.com")
    partner = _partner(db_session, terms, owner)
    attribution = _attribution(db_session, terms, partner, customer)
    paid_before_attribution = int(
        (attribution.attributed_at - timedelta(days=1))
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )

    entry = record_paid_invoice(
        db_session,
        user=customer,
        invoice=_invoice("in_before_attribution", created=paid_before_attribution),
        stripe_event_id="evt_before_attribution",
    )

    assert entry is None
    decision = (
        db_session.query(AffiliateAuditEvent)
        .filter(
            AffiliateAuditEvent.event_type == "commission.invoice_before_attribution",
            AffiliateAuditEvent.source_ref == "in_before_attribution",
        )
        .one()
    )
    assert decision.subject_user_id == customer.id


def test_database_reconciliation_detects_projection_corruption(db_session) -> None:
    from app.services.affiliate_reconciliation import database_reconciliation_report

    terms = _terms(db_session)
    owner = _user(db_session, "reconcile-owner@example.com")
    customer = _user(db_session, "reconcile-customer@example.com")
    partner = _partner(db_session, terms, owner)
    _acceptance(db_session, partner, owner)
    attribution = _attribution(db_session, terms, partner, customer)
    paid_after_attribution = int(
        (attribution.attributed_at + timedelta(minutes=1))
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )
    entry = record_paid_invoice(
        db_session,
        user=customer,
        invoice=_invoice("in_reconcile_ok", created=paid_after_attribution),
        stripe_event_id="evt_reconcile_ok",
    )
    assert entry is not None
    assert database_reconciliation_report(db_session)["status"] == "ok"

    state = (
        db_session.query(AffiliateCommissionState)
        .filter(AffiliateCommissionState.stripe_invoice_id == "in_reconcile_ok")
        .one()
    )
    state.projected_minor += 1
    db_session.commit()
    report = database_reconciliation_report(db_session)
    assert report["status"] == "critical"
    assert "invoice_projection_mismatch" in {issue["code"] for issue in report["issues"]}


def test_stripe_reconciliation_finds_paid_invoice_without_a_decision(db_session) -> None:
    from app.services.affiliate_reconciliation import stripe_reconciliation_report

    terms = _terms(db_session)
    owner = _user(db_session, "scan-owner@example.com")
    customer = _user(db_session, "scan-customer@example.com")
    customer.stripe_subscription_id = "sub_missing_paid_invoice"
    partner = _partner(db_session, terms, owner)
    _acceptance(db_session, partner, owner)
    attribution = _attribution(db_session, terms, partner, customer)
    created = int(
        (attribution.attributed_at + timedelta(minutes=1))
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )
    invoice = {
        **_invoice("in_missing_paid_decision", created=created),
        "subscription": customer.stripe_subscription_id,
        "status": "paid",
    }

    class Page:
        def auto_paging_iter(self):
            return iter([invoice])

    class StripeInvoice:
        @staticmethod
        def list(**_kwargs):
            return Page()

    class StripeTransfer:
        @staticmethod
        def retrieve(_transfer_id):
            raise AssertionError("No transfer should be retrieved")

    class FakeStripe:
        Invoice = StripeInvoice
        Transfer = StripeTransfer

    report = stripe_reconciliation_report(db_session, FakeStripe, invoice_limit=20)
    assert report["status"] == "critical"
    assert report["checked"]["stripe_paid_invoices_scanned"] == 1
    assert "paid_invoice_missing_decision" in {
        issue["code"] for issue in report["issues"]
    }


def test_invoice_backfill_is_previewed_mfa_gated_and_idempotent(
    api_client,
    db_session,
    monkeypatch,
) -> None:
    import app.api.routes.affiliates as affiliate_routes

    terms = _terms(db_session)
    owner = _user(db_session, "backfill-owner@example.com")
    customer = _user(db_session, "backfill-customer@example.com")
    customer.stripe_subscription_id = "sub_affiliate_backfill"
    admin = _user(db_session, "backfill-admin@example.com", role="admin")
    partner = _partner(db_session, terms, owner)
    _acceptance(db_session, partner, owner)
    attribution = _attribution(db_session, terms, partner, customer)
    db_session.add(
        UserMFAMethod(
            user_id=admin.id,
            method_type="totp",
            secret_encrypted="test-only-encrypted-secret",
            verified_at=utcnow(),
        )
    )
    db_session.commit()
    created = int(
        (attribution.attributed_at + timedelta(minutes=1))
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )
    invoice = {
        **_invoice("in_backfill_controlled", paid=10_000, excluding_tax=9_000, created=created),
        "subscription": customer.stripe_subscription_id,
        "status": "paid",
    }
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_not_real")
    monkeypatch.setattr(affiliate_routes.stripe.Invoice, "retrieve", lambda *_a, **_k: invoice)

    preview = api_client.login(admin).post(
        "/admin/affiliates/reconciliation/invoice",
        json={"invoice_id": "in_backfill_controlled", "apply": False},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["applied"] is False
    assert preview.json()["existing_entry_id"] is None
    assert preview.json()["eligible_for_backfill"] is True
    assert preview.json()["decision"] == "ready"

    applied = api_client.post(
        "/admin/affiliates/reconciliation/invoice",
        json={"invoice_id": "in_backfill_controlled", "apply": True},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["applied"] is True
    entry_id = applied.json()["entry"]["id"]

    replay = api_client.post(
        "/admin/affiliates/reconciliation/invoice",
        json={"invoice_id": "in_backfill_controlled", "apply": True},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["applied"] is False
    assert replay.json()["existing_entry_id"] == entry_id
    assert replay.json()["eligible_for_backfill"] is False
    assert replay.json()["decision"] == "existing_ledger"
    assert db_session.query(AffiliateCommissionEntry).filter(
        AffiliateCommissionEntry.stripe_invoice_id == "in_backfill_controlled"
    ).count() == 1
    assert db_session.query(AffiliateAuditEvent).filter(
        AffiliateAuditEvent.event_type == "reconciliation.invoice_backfill",
        AffiliateAuditEvent.source_ref == "in_backfill_controlled",
    ).count() == 1


def test_invoice_backfill_refuses_an_unattributed_account(
    api_client,
    db_session,
    monkeypatch,
) -> None:
    import app.api.routes.affiliates as affiliate_routes

    customer = _user(db_session, "backfill-unattributed@example.com")
    customer.stripe_subscription_id = "sub_unattributed_backfill"
    admin = _user(db_session, "backfill-unattributed-admin@example.com", role="admin")
    db_session.add(
        UserMFAMethod(
            user_id=admin.id,
            method_type="totp",
            secret_encrypted="test-only-encrypted-secret",
            verified_at=utcnow(),
        )
    )
    db_session.commit()
    invoice = {
        **_invoice("in_unattributed_backfill"),
        "subscription": customer.stripe_subscription_id,
        "status": "paid",
    }
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_not_real")
    monkeypatch.setattr(affiliate_routes.stripe.Invoice, "retrieve", lambda *_a, **_k: invoice)

    response = api_client.login(admin).post(
        "/admin/affiliates/reconciliation/invoice",
        json={"invoice_id": "in_unattributed_backfill", "apply": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["eligible_for_backfill"] is False
    assert response.json()["decision"] == "no_active_attribution"
    assert db_session.query(AffiliateCommissionEntry).filter(
        AffiliateCommissionEntry.stripe_invoice_id == "in_unattributed_backfill"
    ).count() == 0


def test_ledger_csv_neutralizes_spreadsheet_formulas(api_client, db_session) -> None:
    terms = _terms(db_session)
    owner = _user(db_session, "csv-owner@example.com")
    customer = _user(db_session, "csv-customer@example.com")
    partner = _partner(db_session, terms, owner)
    attribution = _attribution(db_session, terms, partner, customer)
    created = int(
        (attribution.attributed_at + timedelta(minutes=1))
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )
    entry = record_paid_invoice(
        db_session,
        user=customer,
        invoice=_invoice("in_csv_safe", created=created),
        stripe_event_id="evt_csv_safe",
    )
    assert entry is not None
    entry.description = "=HYPERLINK(\"https://example.invalid\")"
    db_session.commit()

    response = api_client.login(owner).get("/affiliates/me/ledger.csv")
    assert response.status_code == 200, response.text
    assert "'=HYPERLINK" in response.text
    assert "text/csv" in response.headers["content-type"]


def test_payout_statement_is_scoped_to_partner_and_available_to_admin(
    api_client,
    db_session,
) -> None:
    terms = _terms(db_session)
    owner = _user(db_session, "statement-owner@example.com")
    customer = _user(db_session, "statement-customer@example.com")
    finance = _user(db_session, "statement-finance@example.com", role="admin")
    outsider = _user(db_session, "statement-outsider@example.com")
    partner = _partner(db_session, terms, owner)
    attribution = _attribution(db_session, terms, partner, customer)
    created = int(
        (attribution.attributed_at + timedelta(minutes=1))
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )
    entry = record_paid_invoice(
        db_session,
        user=customer,
        invoice=_invoice("in_statement", paid=20_000, excluding_tax=20_000, created=created),
        stripe_event_id="evt_statement",
    )
    assert entry is not None
    entry.available_at = utcnow() - timedelta(minutes=1)
    db_session.commit()
    payout = create_payout_batch(db_session, partner=partner, admin=finance)

    partner_response = api_client.login(owner).get(
        f"/affiliates/me/payouts/{payout.id}/statement.csv"
    )
    assert partner_response.status_code == 200, partner_response.text
    assert f"{payout.id},draft" in partner_response.text
    assert f",{entry.id}," in partner_response.text

    assert api_client.login(outsider).get(
        f"/affiliates/me/payouts/{payout.id}/statement.csv"
    ).status_code == 404
    admin_response = api_client.login(finance).get(
        f"/admin/affiliates/payouts/{payout.id}/statement.csv"
    )
    assert admin_response.status_code == 200, admin_response.text
    assert admin_response.text == partner_response.text


def test_affiliate_privacy_retention_scrubs_correlators_but_keeps_evidence(
    db_session,
    monkeypatch,
) -> None:
    from app.services.affiliate_privacy import apply_affiliate_privacy_retention

    monkeypatch.setenv("AFFILIATE_CLICK_DETAIL_RETENTION_DAYS", "90")
    monkeypatch.setenv("AFFILIATE_ACCEPTANCE_HASH_RETENTION_DAYS", "90")
    now = utcnow()
    terms = _terms(db_session)
    owner = _user(db_session, "privacy-owner@example.com")
    partner = _partner(db_session, terms, owner)
    acceptance = _acceptance(db_session, partner, owner)
    acceptance.accepted_at = now - timedelta(days=91)
    click = AffiliateClick(
        partner_id=partner.id,
        token="privacy-retention-token-abcdefghijklmnopqrstuvwxyz",
        campaign="launch-campaign",
        landing_path="/signup",
        referrer_host="partner.example.com",
        ip_hash="hashed-network-signal",
        user_agent_hash="hashed-browser-signal",
        risk_flags=["ip_velocity"],
        occurred_at=now - timedelta(days=91),
        expires_at=now - timedelta(days=31),
    )
    db_session.add(click)
    db_session.commit()
    original_click_id = click.id
    original_token = click.token

    result = apply_affiliate_privacy_retention(db_session, now=now)
    db_session.refresh(click)
    db_session.refresh(acceptance)
    assert result == {
        "clicks_scrubbed": 1,
        "acceptances_scrubbed": 1,
        "referral_invite_emails_scrubbed": 0,
    }
    assert click.id == original_click_id
    assert click.partner_id == partner.id
    assert click.occurred_at is not None
    assert click.token != original_token and click.token.startswith("retired_")
    assert click.campaign is None
    assert click.landing_path is None
    assert click.referrer_host is None
    assert click.ip_hash is None
    assert click.user_agent_hash is None
    assert click.risk_flags is None
    assert click.privacy_scrubbed_at == now
    assert acceptance.ip_hash is None
    assert acceptance.user_agent_hash is None
    assert apply_affiliate_privacy_retention(db_session, now=now) == {
        "clicks_scrubbed": 0,
        "acceptances_scrubbed": 0,
        "referral_invite_emails_scrubbed": 0,
    }


def test_partner_dashboard_groups_campaign_performance(db_session) -> None:
    terms = _terms(db_session)
    owner = _user(db_session, "campaign-owner@example.com")
    customer = _user(db_session, "campaign-customer@example.com")
    partner = _partner(db_session, terms, owner)
    campaign = AffiliateCampaign(
        partner_id=partner.id,
        slug="launch-video",
        name="Launch video",
        destination_path="/signup",
        status="active",
    )
    db_session.add(campaign)
    db_session.flush()
    attribution = _attribution(db_session, terms, partner, customer)
    click = db_session.query(AffiliateClick).filter(
        AffiliateClick.id == attribution.click_id
    ).one()
    click.campaign = campaign.slug
    click.campaign_id = campaign.id
    db_session.commit()
    created = int(
        (attribution.attributed_at + timedelta(minutes=1))
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )
    entry = record_paid_invoice(
        db_session,
        user=customer,
        invoice=_invoice("in_campaign", created=created),
        stripe_event_id="evt_campaign",
    )
    assert entry is not None

    dashboard = partner_dashboard(db_session, partner)
    assert len(dashboard["campaigns"]) == 1
    result = dashboard["campaigns"][0]
    assert result["id"] == campaign.id
    assert result["campaign"] == "launch-video"
    assert result["name"] == "Launch video"
    assert result["status"] == "active"
    assert result["clicks"] == 1
    assert result["referrals"] == 1
    assert result["customers"] == 1
    assert result["commission_minor"] == entry.amount_minor


def test_admin_can_reconcile_a_partner_ledger(api_client, db_session) -> None:
    terms = _terms(db_session)
    owner = _user(db_session, "ledger-owner@example.com")
    customer = _user(db_session, "ledger-customer@example.com")
    admin = _user(db_session, "ledger-admin@example.com", role="admin")
    partner = _partner(db_session, terms, owner)
    _attribution(db_session, terms, partner, customer)
    entry = record_paid_invoice(
        db_session,
        user=customer,
        invoice=_invoice("in_admin_ledger", paid=10_000, excluding_tax=9_000),
        stripe_event_id="evt_admin_ledger",
    )
    assert entry is not None
    entry.available_at = utcnow() - timedelta(minutes=1)
    db_session.commit()

    response = api_client.login(admin).get(
        f"/admin/affiliates/partners/{partner.id}/ledger"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["currency"] == "usd"
    assert body["payable_minor"] == 2700
    assert body["entries"] == [
        {
            "id": entry.id,
            "entry_type": "accrual",
            "stripe_invoice_id": "in_admin_ledger",
            "amount_minor": 2700,
            "commissionable_minor": 9000,
            "rate_bps": 3000,
            "currency": "usd",
            "available_at": entry.available_at.isoformat(),
            "description": "Commission on collected subscription revenue excluding tax",
            "created_at": entry.created_at.isoformat(),
        }
    ]


def test_financial_operations_require_verified_mfa(api_client, db_session) -> None:
    admin = _user(db_session, "admin-without-mfa@example.com", role="admin")
    response = api_client.login(admin).post(
        "/admin/affiliates/adjustments",
        json={
            "partner_id": 1,
            "attribution_id": 1,
            "amount_minor": 100,
            "reason": "Finance correction that must never bypass the MFA control.",
            "idempotency_key": "mfa-gate-test-12345",
        },
    )
    assert response.status_code == 403
    assert "two-factor" in response.json()["detail"].lower()


def test_clearing_partner_risk_requires_a_resolution_record(api_client, db_session) -> None:
    terms = _terms(db_session)
    owner = _user(db_session, "risk-review-owner@example.com")
    admin = _user(db_session, "risk-review-admin@example.com", role="admin")
    partner = _partner(db_session, terms, owner)
    partner.risk_status = "review"
    partner.hold_reason = "Tax and sanctions review is still pending."
    db_session.add(
        UserMFAMethod(
            user_id=admin.id,
            method_type="totp",
            secret_encrypted="test-only-encrypted-secret",
            verified_at=utcnow(),
        )
    )
    db_session.commit()

    missing_note = api_client.login(admin).patch(
        f"/admin/affiliates/partners/{partner.id}",
        json={"risk_status": "clear", "hold_reason": None},
    )
    assert missing_note.status_code == 400
    cleared = api_client.login(admin).patch(
        f"/admin/affiliates/partners/{partner.id}",
        json={
            "risk_status": "clear",
            "hold_reason": None,
            "risk_review_note": "Identity, tax profile, and sanctions screening were reviewed.",
        },
    )
    assert cleared.status_code == 200, cleared.text
    db_session.refresh(partner)
    assert partner.risk_status == "clear"
    assert partner.hold_reason is None


def test_custom_terms_cannot_reduce_an_accepted_commercial_floor(api_client, db_session) -> None:
    terms = _terms(db_session)
    owner = _user(db_session, "commercial-floor-owner@example.com")
    admin = _user(db_session, "commercial-floor-admin@example.com", role="admin")
    partner = _partner(db_session, terms, owner)
    db_session.add(
        UserMFAMethod(
            user_id=admin.id,
            method_type="totp",
            secret_encrypted="test-only-encrypted-secret",
            verified_at=utcnow(),
        )
    )
    db_session.commit()

    reduced = api_client.login(admin).patch(
        f"/admin/affiliates/partners/{partner.id}",
        json={"custom_commission_rate_bps": 2500},
    )
    assert reduced.status_code == 409
    increased = api_client.login(admin).patch(
        f"/admin/affiliates/partners/{partner.id}",
        json={
            "custom_commission_rate_bps": 3500,
            "custom_commission_months": 18,
        },
    )
    assert increased.status_code == 200, increased.text
    db_session.refresh(partner)
    assert partner.custom_commission_rate_bps == 3500
    assert partner.custom_commission_months == 18


def test_stripe_webhook_accrues_and_reverses_affiliate_commission(
    api_client,
    db_session,
    monkeypatch,
) -> None:
    import app.api.routes.billing as billing

    terms = _terms(db_session)
    owner = _user(db_session, "webhook-owner@example.com")
    customer = _user(db_session, "webhook-customer@example.com")
    customer.stripe_subscription_id = "sub_affiliate_webhook"
    customer.stripe_customer_id = "cus_affiliate_webhook"
    partner = _partner(db_session, terms, owner)
    _attribution(db_session, terms, partner, customer)
    db_session.commit()

    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(billing.stripe, "api_key", "sk_test_fake")
    invoice = {
        **_invoice("in_webhook", paid=1000, excluding_tax=900),
        "subscription": "sub_affiliate_webhook",
        "customer": "cus_affiliate_webhook",
    }
    full_invoice_lines = invoice["lines"]["data"]
    invoice["lines"] = {"has_more": True, "data": []}

    class CompleteLinePage:
        def auto_paging_iter(self):
            return iter(full_invoice_lines)

    monkeypatch.setattr(
        billing.stripe.Invoice,
        "list_lines",
        lambda *_args, **_kwargs: CompleteLinePage(),
    )

    def post(event_id: str, event_type: str, obj: dict):
        event = {"id": event_id, "type": event_type, "data": {"object": obj}}
        monkeypatch.setattr(billing.stripe.Webhook, "construct_event", lambda *a, **k: event)
        return api_client.post(
            "/billing/webhook",
            content=json.dumps({"id": event_id}),
            headers={"stripe-signature": "t=1,v1=fake"},
        )

    refunded_charge = {
        "id": "ch_in_webhook",
        "invoice": "in_webhook",
        "amount_refunded": 1000,
    }
    monkeypatch.setattr(billing.stripe.Invoice, "retrieve", lambda *a, **k: invoice)
    with pytest.raises(RuntimeError, match="has not completed"):
        post("evt_affiliate_refund", "charge.refunded", refunded_charge)

    paid = post("evt_affiliate_paid", "invoice.paid", invoice)
    assert paid.status_code == 200, paid.text
    assert db_session.query(AffiliateCommissionEntry).filter(
        AffiliateCommissionEntry.stripe_invoice_id == "in_webhook"
    ).count() == 1
    replay = post("evt_affiliate_paid", "invoice.paid", invoice)
    assert replay.json().get("duplicate") is True

    refunded = post(
        "evt_affiliate_refund",
        "charge.refunded",
        refunded_charge,
    )
    assert refunded.status_code == 200, refunded.text
    entries = db_session.query(AffiliateCommissionEntry).filter(
        AffiliateCommissionEntry.stripe_invoice_id == "in_webhook"
    ).all()
    assert sum(entry.amount_minor for entry in entries) == 0


def test_active_terms_accept_distinct_launch_approvals_and_fail_closed_on_revoke(
    db_session,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AFFILIATE_LEGAL_APPROVED", "1")
    terms = AffiliateProgramTerms(
        version="active-approval-backfill",
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
    db_session.add(terms)
    db_session.commit()
    approvers = [
        _user(db_session, f"launch-{role}@example.com", role="admin")
        for role in ("legal", "finance", "product", "engineering")
    ]
    record_launch_approval(
        db_session,
        terms=terms,
        role="legal",
        admin=approvers[0],
        note="Counsel approved this immutable terms checksum for launch.",
    )
    with pytest.raises(AffiliateProgramError, match="different verified administrator"):
        record_launch_approval(
            db_session,
            terms=terms,
            role="finance",
            admin=approvers[0],
            note="Finance approved the payout economics and controls.",
        )
    for role, admin in zip(("finance", "product", "engineering"), approvers[1:]):
        record_launch_approval(
            db_session,
            terms=terms,
            role=role,
            admin=admin,
            note=f"The {role} launch owner approved the production controls.",
        )
    assert launch_approval_state(db_session, terms)["ready"] is True
    assert terms_launch_ready(db_session, terms) is True

    legal = db_session.query(AffiliateLaunchApproval).filter(
        AffiliateLaunchApproval.terms_version_id == terms.id,
        AffiliateLaunchApproval.approval_role == "legal",
    ).one()
    legal.revoked_at = utcnow()
    db_session.commit()
    assert terms_launch_ready(db_session, terms) is False


def test_managed_campaign_lifecycle_blocks_unmanaged_held_and_archived_links(
    db_session,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AFFILIATE_LEGAL_APPROVED", "1")
    terms = _terms(db_session)
    owner = _user(db_session, "campaign-lifecycle@example.com")
    partner = _partner(db_session, terms, owner)
    campaign = create_partner_campaign(
        db_session,
        partner=partner,
        name="Launch newsletter",
        slug="launch-newsletter",
        destination_path="/partners/affiliate",
    )
    click = record_click(
        db_session,
        code=partner.code,
        campaign=campaign.slug,
        landing_path=campaign.destination_path,
        referrer=None,
        ip="203.0.113.70",
        user_agent="TestBrowser/1",
    )
    assert click.campaign_id == campaign.id
    with pytest.raises(AffiliateProgramError, match="not active"):
        record_click(
            db_session,
            code=partner.code,
            campaign="unmanaged-tag",
            landing_path="/signup",
            referrer=None,
            ip=None,
            user_agent=None,
        )
    update_partner_campaign(db_session, partner=partner, campaign=campaign, status="paused")
    partner.risk_status = "held"
    db_session.commit()
    with pytest.raises(AffiliateProgramError, match="partner hold"):
        update_partner_campaign(db_session, partner=partner, campaign=campaign, status="active")
    partner.risk_status = "clear"
    db_session.commit()
    update_partner_campaign(db_session, partner=partner, campaign=campaign, status="archived")
    with pytest.raises(AffiliateProgramError, match="immutable"):
        update_partner_campaign(db_session, partner=partner, campaign=campaign, name="Renamed")


def test_attribution_identity_matches_hold_partner_for_review(db_session) -> None:
    terms = _terms(db_session)
    owner = _user(db_session, "owner@studio.example")
    customer = _user(db_session, "buyer@studio.example")
    owner.stripe_customer_id = "cus_shared_identity"
    customer.stripe_customer_id = "cus_shared_identity"
    db_session.commit()
    partner = _partner(db_session, terms, owner)
    click = AffiliateClick(
        partner_id=partner.id,
        token="risk-identity-token-abcdefghijklmnopqrstuvwxyz",
        occurred_at=utcnow(),
        expires_at=utcnow() + timedelta(days=60),
    )
    db_session.add(click)
    db_session.commit()
    attribution = claim_attribution(db_session, user=customer, click_token=click.token)
    assert set(attribution.risk_flags) == {
        "shared_business_email_domain",
        "shared_stripe_customer",
    }
    db_session.refresh(partner)
    assert partner.risk_status == "review"
    assert partner.hold_reason


def test_payout_snapshots_gross_withholding_and_net(db_session) -> None:
    terms = _terms(db_session)
    owner = _user(db_session, "withholding-owner@example.com")
    customer = _user(db_session, "withholding-customer@example.com")
    finance = _user(db_session, "withholding-finance@example.com", role="admin")
    partner = _partner(db_session, terms, owner)
    profile = db_session.query(AffiliateComplianceProfile).filter(
        AffiliateComplianceProfile.partner_id == partner.id
    ).one()
    profile.withholding_rate_bps = 1250
    _attribution(db_session, terms, partner, customer)
    entry = record_paid_invoice(
        db_session,
        user=customer,
        invoice=_invoice("in_withholding", paid=20_000, excluding_tax=20_000),
        stripe_event_id="evt_withholding",
    )
    assert entry is not None
    entry.available_at = utcnow() - timedelta(minutes=1)
    db_session.commit()
    payout = create_payout_batch(db_session, partner=partner, admin=finance)
    assert payout.gross_amount_minor == 6000
    assert payout.withholding_rate_bps == 1250
    assert payout.withholding_minor == 750
    assert payout.amount_minor == 5250


def test_referral_dispute_win_and_loss_reconcile_credit_ledger(db_session) -> None:
    from app.services.affiliate_reconciliation import database_reconciliation_report
    from app.services.referrals import (
        finalize_referral_reward_after_lost_dispute,
        reinstate_referral_reward_after_dispute,
        reverse_referral_reward_for_dispute,
        reward_referral_from_paid_invoice,
    )

    referrer = _user(db_session, "dispute-referrer@example.com")
    invitee = _user(db_session, "dispute-invitee@example.com")
    code = ReferralCode(user_id=referrer.id, code="DISPUTE2", passes_total=3)
    db_session.add(code)
    db_session.flush()
    referral = Referral(
        referrer_user_id=referrer.id,
        referral_code_id=code.id,
        code=code.code,
        invitee_user_id=invitee.id,
        invitee_email=invitee.email,
        status="signed_up",
        signed_up_at=utcnow(),
        pass_trial_days=30,
    )
    db_session.add(referral)
    db_session.commit()
    reward_referral_from_paid_invoice(db_session, invitee, "in_referral_dispute")
    reverse_referral_reward_for_dispute(
        db_session,
        stripe_invoice_id="in_referral_dispute",
        stripe_charge_id="ch_referral_dispute_one",
    )
    db_session.refresh(referral)
    assert referral.reward_dispute_active is True
    assert database_reconciliation_report(db_session)["status"] == "ok"

    reinstate_referral_reward_after_dispute(
        db_session,
        stripe_invoice_id="in_referral_dispute",
        stripe_charge_id="ch_referral_dispute_one",
    )
    reverse_referral_reward_for_dispute(
        db_session,
        stripe_invoice_id="in_referral_dispute",
        stripe_charge_id="ch_referral_dispute_two",
    )
    finalize_referral_reward_after_lost_dispute(
        db_session,
        stripe_invoice_id="in_referral_dispute",
    )
    db_session.refresh(referral)
    assert referral.void_reason == "dispute_lost"
    assert referral.reward_dispute_active is False
    assert referral.reward_reversed_at is not None
    assert sum(
        item.delta
        for item in db_session.query(AccountCreditLedger).filter(
            AccountCreditLedger.user_id == referrer.id
        )
    ) == 0
    assert database_reconciliation_report(db_session)["status"] == "ok"


def test_bounce_suppression_is_idempotent_and_releases_invite_capacity(
    db_session,
    monkeypatch,
) -> None:
    from app.services.referrals import record_referral_email_event, send_email_invite

    monkeypatch.setenv("REFERRAL_EMAIL_HASH_SECRET", "stable-test-suppression-secret")
    sender = _user(db_session, "bounce-sender@example.com")
    referral = send_email_invite(db_session, sender, "bounce-target@example.com")
    event = record_referral_email_event(
        db_session,
        provider_event_id="provider-bounce-1",
        email="BOUNCE-target@example.com",
        event_type="bounce",
        occurred_at=utcnow(),
    )
    replay = record_referral_email_event(
        db_session,
        provider_event_id="provider-bounce-1",
        email="bounce-target@example.com",
        event_type="delivered",
        occurred_at=utcnow(),
    )
    db_session.refresh(referral)
    assert event == {"recorded": True, "suppressed": True}
    assert replay == {"recorded": False, "suppressed": True}
    assert referral.capacity_released_at is not None
    assert referral.capacity_release_reason == "email_bounce"
    assert db_session.query(ReferralEmailEvent).count() == 1
    suppression = db_session.query(ReferralEmailSuppression).one()
    assert suppression.email_hash != "bounce-target@example.com"
    latest = db_session.query(ReferralInviteDelivery).filter(
        ReferralInviteDelivery.referral_id == referral.id
    ).order_by(ReferralInviteDelivery.attempt_number.desc()).first()
    assert latest.status == "suppressed"
    assert latest.next_retry_at is None


def test_existing_account_invite_releases_capacity_even_when_email_fails(db_session) -> None:
    from app.services.referrals import send_email_invite

    sender = _user(db_session, "existing-sender@example.com")
    _user(db_session, "existing-target@example.com")
    referral = send_email_invite(db_session, sender, "existing-target@example.com")
    db_session.refresh(referral)
    assert referral.status == "invited"
    assert referral.capacity_release_reason == "existing_account"
    assert referral.capacity_released_at is not None


def test_referral_privacy_sweep_scrubs_expired_unclaimed_email(db_session, monkeypatch) -> None:
    from app.services.affiliate_privacy import apply_affiliate_privacy_retention

    monkeypatch.setenv("REFERRAL_INVITE_EMAIL_RETENTION_DAYS", "7")
    sender = _user(db_session, "privacy-referral-sender@example.com")
    code = ReferralCode(user_id=sender.id, code="PRIVACY2", passes_total=3)
    db_session.add(code)
    db_session.flush()
    referral = Referral(
        referrer_user_id=sender.id,
        referral_code_id=code.id,
        code=code.code,
        invitee_email="expired-private@example.com",
        status="invited",
        pass_trial_days=30,
        invited_at=utcnow() - timedelta(days=30),
        invite_expires_at=utcnow() - timedelta(days=8),
    )
    db_session.add(referral)
    db_session.commit()
    result = apply_affiliate_privacy_retention(db_session, now=utcnow())
    db_session.refresh(referral)
    assert result["referral_invite_emails_scrubbed"] == 1
    assert referral.invitee_email is None
    assert referral.status == "expired"


def test_admin_code_revocation_expires_invites_cancels_retry_and_audits(
    api_client,
    db_session,
) -> None:
    sender = _user(db_session, "revocation-sender@example.com")
    admin = _user(db_session, "revocation-admin@example.com", role="admin")
    db_session.add(
        UserMFAMethod(
            user_id=admin.id,
            method_type="totp",
            secret_encrypted="test-only-encrypted-secret",
            verified_at=utcnow(),
        )
    )
    code = ReferralCode(user_id=sender.id, code="REVOKE22", passes_total=3)
    db_session.add(code)
    db_session.flush()
    referral = Referral(
        referrer_user_id=sender.id,
        referral_code_id=code.id,
        code=code.code,
        invitee_email="pending-revoke@example.com",
        status="invited",
        pass_trial_days=30,
        invited_at=utcnow(),
        invite_expires_at=utcnow() + timedelta(days=14),
    )
    db_session.add(referral)
    db_session.flush()
    delivery = ReferralInviteDelivery(
        referral_id=referral.id,
        referrer_user_id=sender.id,
        attempt_number=1,
        status="failed",
        retry_count=0,
        next_retry_at=utcnow() + timedelta(minutes=5),
    )
    db_session.add(delivery)
    db_session.commit()
    response = api_client.login(admin).patch(
        f"/admin/referrals/codes/{code.id}",
        json={
            "revoked": True,
            "reason": "Confirmed abuse investigation requires immediate code shutdown.",
        },
    )
    assert response.status_code == 200, response.text
    db_session.refresh(referral)
    db_session.refresh(delivery)
    assert referral.status == "expired"
    assert delivery.next_retry_at is None
    event = db_session.query(ReferralAdminAuditEvent).filter(
        ReferralAdminAuditEvent.event_type == "code.updated"
    ).one()
    assert event.payload["pending_invites_expired"] == 1
