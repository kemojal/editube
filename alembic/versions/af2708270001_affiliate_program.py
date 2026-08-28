"""add the versioned cash affiliate program

Revision ID: af2708270001
Revises: c7d8e9f0a1b2
Create Date: 2026-08-27 05:10:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "af2708270001"
down_revision = "c7d8e9f0a1b2"
branch_labels = None
depends_on = None


V1_LEGAL_TEXT = """Editube Affiliate Program Terms

Eligibility and acceptance. Participation is application-only. Approval is at
Editube's discretion. A partner may promote Editube only after accepting the
published version of these terms and may not assign or transfer their account.

Attribution. Editube uses the first eligible affiliate click recorded within the
attribution window in the versioned Commercial Summary. Self-referrals,
fabricated identities, cookie stuffing, forced redirects, misleading claims,
trademark bidding, unsolicited messages, and interference with another
partner's attribution are prohibited. Affiliate attribution does not stack with
the refer-a-friend guest-pass program; when both are presented at signup, the
guest pass takes precedence.

Commission. The applicable rate and eligibility period are stated in the
versioned Commercial Summary and run from the referred customer's first paid
invoice. Commission applies only to eligible subscription cash actually
collected, after discounts and excluding taxes, refunds, disputes, chargebacks,
credits, and non-subscription charges. Editube's signed ledger controls where
third-party reports differ.

Payout. The payout currency, hold period, and minimum payable balance are stated
in the versioned Commercial Summary. Identity, tax, sanctions, and Stripe
Connect verification must be complete. Negative adjustments carry forward and
may be offset against later commissions.

Changes and termination. New commercial terms require a new version and fresh
acceptance where they materially affect an existing partner. Editube may hold
or suspend activity while investigating abuse, and may close participation for
breach. Earned, non-fraudulent balances remain subject to refunds, disputes,
verification, and applicable law.

Content and law. Partners must use accurate, lawful disclosures and approved
brand assets. They are independent contractors and have no authority to bind
Editube. Confidential information and customer personal data must be protected.
The program is unavailable where prohibited by law.
"""
V1_LEGAL_CHECKSUM = "f4dc3eed97c7bcd418983793c67501160f5c88f2a8f3b01723dfc05bd40388a4"


def upgrade() -> None:
    op.add_column("referrals", sa.Column("reward_source_invoice_id", sa.String(), nullable=True))
    op.add_column("referrals", sa.Column("reward_reversed_at", sa.TIMESTAMP(), nullable=True))
    op.create_index(
        "ix_referrals_reward_source_invoice_id",
        "referrals",
        ["reward_source_invoice_id"],
    )

    op.create_table(
        "referral_invite_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("referral_id", sa.Integer(), nullable=False),
        sa.Column("referrer_user_id", sa.Integer(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), server_default="queued", nullable=False),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("sent_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["referral_id"], ["referrals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["referrer_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint("status IN ('queued','sent','failed')", name="ck_referral_delivery_status"),
        sa.UniqueConstraint("referral_id", "attempt_number", name="uq_referral_delivery_attempt"),
    )
    op.create_index("ix_referral_invite_deliveries_referral_id", "referral_invite_deliveries", ["referral_id"])
    op.create_index("ix_referral_invite_deliveries_referrer_user_id", "referral_invite_deliveries", ["referrer_user_id"])
    op.create_index("ix_referral_invite_deliveries_status", "referral_invite_deliveries", ["status"])
    op.create_index("ix_referral_invite_deliveries_created_at", "referral_invite_deliveries", ["created_at"])

    op.create_table(
        "affiliate_program_terms",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default="draft", nullable=False),
        sa.Column("commission_rate_bps", sa.Integer(), server_default="3000", nullable=False),
        sa.Column("commission_months", sa.Integer(), server_default="12", nullable=False),
        sa.Column("attribution_window_days", sa.Integer(), server_default="60", nullable=False),
        sa.Column("payout_minimum_minor", sa.BigInteger(), server_default="5000", nullable=False),
        sa.Column("hold_days", sa.Integer(), server_default="30", nullable=False),
        sa.Column("currency", sa.String(), server_default="usd", nullable=False),
        sa.Column(
            "commission_basis",
            sa.String(),
            server_default="invoice_amount_paid_excluding_tax",
            nullable=False,
        ),
        sa.Column("legal_text", sa.Text(), nullable=False),
        sa.Column("legal_copy_checksum", sa.String(), nullable=False),
        sa.Column("effective_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("retired_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("status IN ('draft','active','retired')", name="ck_affiliate_terms_status"),
        sa.CheckConstraint("commission_rate_bps BETWEEN 0 AND 10000", name="ck_affiliate_terms_rate"),
        sa.CheckConstraint("commission_months BETWEEN 1 AND 60", name="ck_affiliate_terms_months"),
        sa.CheckConstraint("attribution_window_days BETWEEN 1 AND 365", name="ck_affiliate_terms_window"),
        sa.CheckConstraint("hold_days BETWEEN 0 AND 180", name="ck_affiliate_terms_hold"),
        sa.UniqueConstraint("version"),
    )
    op.create_index("ix_affiliate_program_terms_version", "affiliate_program_terms", ["version"])
    op.create_index("ix_affiliate_program_terms_status", "affiliate_program_terms", ["status"])

    op.create_table(
        "affiliate_applications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("business_name", sa.String(), nullable=True),
        sa.Column("website_url", sa.String(), nullable=True),
        sa.Column("country_code", sa.String(), nullable=False),
        sa.Column("audience_description", sa.Text(), nullable=False),
        sa.Column("audience_size", sa.Integer(), nullable=True),
        sa.Column("promotion_channels", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payout_currency", sa.String(), server_default="usd", nullable=False),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column("applicant_attested_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("reviewed_by_user_id", sa.Integer(), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("status IN ('pending','approved','rejected','withdrawn')", name="ck_affiliate_application_status"),
        sa.CheckConstraint("audience_size IS NULL OR audience_size >= 0", name="ck_affiliate_application_audience"),
    )
    op.create_index("ix_affiliate_applications_user_id", "affiliate_applications", ["user_id"])
    op.create_index("ix_affiliate_applications_status", "affiliate_applications", ["status"])
    op.create_index(
        "uq_affiliate_application_pending_user",
        "affiliate_applications",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "affiliate_partners",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("terms_version_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default="pending_terms", nullable=False),
        sa.Column("custom_commission_rate_bps", sa.Integer(), nullable=True),
        sa.Column("custom_commission_months", sa.Integer(), nullable=True),
        sa.Column("stripe_connect_account_id", sa.String(), nullable=True),
        sa.Column("payouts_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("risk_status", sa.String(), server_default="review", nullable=False),
        sa.Column("hold_reason", sa.Text(), nullable=True),
        sa.Column("approved_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("suspended_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("closed_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["application_id"], ["affiliate_applications.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["terms_version_id"], ["affiliate_program_terms.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("status IN ('pending_terms','active','suspended','closed')", name="ck_affiliate_partner_status"),
        sa.CheckConstraint("risk_status IN ('clear','review','held')", name="ck_affiliate_partner_risk"),
        sa.CheckConstraint("custom_commission_rate_bps IS NULL OR custom_commission_rate_bps BETWEEN 0 AND 10000", name="ck_affiliate_partner_rate"),
        sa.CheckConstraint("custom_commission_months IS NULL OR custom_commission_months BETWEEN 1 AND 60", name="ck_affiliate_partner_months"),
        sa.UniqueConstraint("user_id"),
        sa.UniqueConstraint("application_id"),
        sa.UniqueConstraint("code"),
        sa.UniqueConstraint("stripe_connect_account_id"),
    )
    for name, cols in (
        ("ix_affiliate_partners_user_id", ["user_id"]),
        ("ix_affiliate_partners_code", ["code"]),
        ("ix_affiliate_partners_status", ["status"]),
        ("ix_affiliate_partners_risk_status", ["risk_status"]),
        ("ix_affiliate_partners_stripe_connect_account_id", ["stripe_connect_account_id"]),
    ):
        op.create_index(name, "affiliate_partners", cols)

    op.create_table(
        "affiliate_terms_acceptances",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("partner_id", sa.Integer(), nullable=False),
        sa.Column("terms_version_id", sa.Integer(), nullable=False),
        sa.Column("accepted_by_user_id", sa.Integer(), nullable=False),
        sa.Column("ip_hash", sa.String(), nullable=True),
        sa.Column("user_agent_hash", sa.String(), nullable=True),
        sa.Column("accepted_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["partner_id"], ["affiliate_partners.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["terms_version_id"], ["affiliate_program_terms.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["accepted_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("partner_id", "terms_version_id", name="uq_affiliate_terms_acceptance"),
    )
    op.create_index("ix_affiliate_terms_acceptances_partner_id", "affiliate_terms_acceptances", ["partner_id"])

    op.create_table(
        "affiliate_clicks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("partner_id", sa.Integer(), nullable=False),
        sa.Column("token", sa.String(), nullable=False),
        sa.Column("campaign", sa.String(), nullable=True),
        sa.Column("landing_path", sa.String(), nullable=True),
        sa.Column("referrer_host", sa.String(), nullable=True),
        sa.Column("ip_hash", sa.String(), nullable=True),
        sa.Column("user_agent_hash", sa.String(), nullable=True),
        sa.Column("risk_flags", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("occurred_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(), nullable=False),
        sa.ForeignKeyConstraint(["partner_id"], ["affiliate_partners.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("token"),
    )
    for name, cols in (
        ("ix_affiliate_clicks_partner_id", ["partner_id"]),
        ("ix_affiliate_clicks_token", ["token"]),
        ("ix_affiliate_clicks_campaign", ["campaign"]),
        ("ix_affiliate_clicks_occurred_at", ["occurred_at"]),
        ("ix_affiliate_clicks_expires_at", ["expires_at"]),
    ):
        op.create_index(name, "affiliate_clicks", cols)

    op.create_table(
        "affiliate_attributions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("partner_id", sa.Integer(), nullable=False),
        sa.Column("click_id", sa.Integer(), nullable=False),
        sa.Column("invitee_user_id", sa.Integer(), nullable=False),
        sa.Column("terms_version_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), server_default="active", nullable=False),
        sa.Column("void_reason", sa.String(), nullable=True),
        sa.Column("attributed_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.Column("first_paid_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("commission_ends_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["partner_id"], ["affiliate_partners.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["click_id"], ["affiliate_clicks.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["invitee_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["terms_version_id"], ["affiliate_program_terms.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("status IN ('active','void')", name="ck_affiliate_attribution_status"),
        sa.UniqueConstraint("invitee_user_id"),
    )
    for name, cols in (
        ("ix_affiliate_attributions_partner_id", ["partner_id"]),
        ("ix_affiliate_attributions_invitee_user_id", ["invitee_user_id"]),
        ("ix_affiliate_attributions_status", ["status"]),
    ):
        op.create_index(name, "affiliate_attributions", cols)

    op.create_table(
        "affiliate_commission_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("partner_id", sa.Integer(), nullable=False),
        sa.Column("attribution_id", sa.Integer(), nullable=False),
        sa.Column("terms_version_id", sa.Integer(), nullable=False),
        sa.Column("source_key", sa.String(), nullable=False),
        sa.Column("source_event_id", sa.String(), nullable=True),
        sa.Column("entry_type", sa.String(), nullable=False),
        sa.Column("stripe_invoice_id", sa.String(), nullable=True),
        sa.Column("stripe_charge_id", sa.String(), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("commissionable_minor", sa.BigInteger(), nullable=False),
        sa.Column("rate_bps", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("available_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["partner_id"], ["affiliate_partners.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["attribution_id"], ["affiliate_attributions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["terms_version_id"], ["affiliate_program_terms.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("entry_type IN ('accrual','refund_reversal','dispute_reversal','dispute_reinstatement','manual_adjustment')", name="ck_affiliate_commission_type"),
        sa.CheckConstraint("rate_bps BETWEEN 0 AND 10000", name="ck_affiliate_commission_rate"),
        sa.UniqueConstraint("source_key"),
    )
    for name, cols in (
        ("ix_affiliate_commission_entries_partner_id", ["partner_id"]),
        ("ix_affiliate_commission_entries_attribution_id", ["attribution_id"]),
        ("ix_affiliate_commission_entries_source_key", ["source_key"]),
        ("ix_affiliate_commission_entries_source_event_id", ["source_event_id"]),
        ("ix_affiliate_commission_entries_entry_type", ["entry_type"]),
        ("ix_affiliate_commission_entries_stripe_invoice_id", ["stripe_invoice_id"]),
        ("ix_affiliate_commission_entries_stripe_charge_id", ["stripe_charge_id"]),
        ("ix_affiliate_commission_entries_currency", ["currency"]),
        ("ix_affiliate_commission_entries_available_at", ["available_at"]),
        ("ix_affiliate_commission_entries_created_at", ["created_at"]),
    ):
        op.create_index(name, "affiliate_commission_entries", cols)

    op.create_table(
        "affiliate_commission_states",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("stripe_invoice_id", sa.String(), nullable=False),
        sa.Column("partner_id", sa.Integer(), nullable=False),
        sa.Column("attribution_id", sa.Integer(), nullable=False),
        sa.Column("accrual_entry_id", sa.Integer(), nullable=False),
        sa.Column("accrued_minor", sa.BigInteger(), nullable=False),
        sa.Column("refund_target_minor", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("refund_target_commissionable_minor", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("dispute_active", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("projected_minor", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["partner_id"], ["affiliate_partners.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["attribution_id"], ["affiliate_attributions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["accrual_entry_id"], ["affiliate_commission_entries.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("refund_target_minor >= 0", name="ck_affiliate_commission_state_refund"),
        sa.CheckConstraint("refund_target_commissionable_minor >= 0", name="ck_affiliate_commission_state_refund_basis"),
        sa.CheckConstraint("projected_minor >= 0", name="ck_affiliate_commission_state_projected"),
        sa.CheckConstraint("accrued_minor > 0", name="ck_affiliate_commission_state_accrued"),
        sa.CheckConstraint("refund_target_minor <= accrued_minor", name="ck_affiliate_commission_state_refund_cap"),
        sa.CheckConstraint("projected_minor <= accrued_minor", name="ck_affiliate_commission_state_projected_cap"),
        sa.UniqueConstraint("stripe_invoice_id"),
        sa.UniqueConstraint("accrual_entry_id"),
    )
    op.create_index("ix_affiliate_commission_states_stripe_invoice_id", "affiliate_commission_states", ["stripe_invoice_id"])
    op.create_index("ix_affiliate_commission_states_partner_id", "affiliate_commission_states", ["partner_id"])

    op.create_table(
        "affiliate_payouts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("partner_id", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("threshold_minor", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(), server_default="draft", nullable=False),
        sa.Column("period_start", sa.TIMESTAMP(), nullable=True),
        sa.Column("period_end", sa.TIMESTAMP(), nullable=False),
        sa.Column("stripe_transfer_id", sa.String(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("approved_by_user_id", sa.Integer(), nullable=True),
        sa.Column("approved_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("processed_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("paid_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["partner_id"], ["affiliate_partners.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["approved_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("status IN ('draft','approved','processing','paid','failed','canceled')", name="ck_affiliate_payout_status"),
        sa.CheckConstraint("amount_minor > 0", name="ck_affiliate_payout_amount"),
        sa.UniqueConstraint("stripe_transfer_id"),
    )
    for name, cols in (
        ("ix_affiliate_payouts_partner_id", ["partner_id"]),
        ("ix_affiliate_payouts_currency", ["currency"]),
        ("ix_affiliate_payouts_status", ["status"]),
        ("ix_affiliate_payouts_stripe_transfer_id", ["stripe_transfer_id"]),
    ):
        op.create_index(name, "affiliate_payouts", cols)

    op.create_table(
        "affiliate_payout_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("payout_id", sa.Integer(), nullable=False),
        sa.Column("commission_entry_id", sa.Integer(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["payout_id"], ["affiliate_payouts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["commission_entry_id"], ["affiliate_commission_entries.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("commission_entry_id", name="uq_affiliate_payout_entry"),
    )
    op.create_index("ix_affiliate_payout_items_payout_id", "affiliate_payout_items", ["payout_id"])

    op.create_table(
        "affiliate_audit_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("partner_id", sa.Integer(), nullable=True),
        sa.Column("subject_user_id", sa.Integer(), nullable=True),
        sa.Column("source_ref", sa.String(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["partner_id"], ["affiliate_partners.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["subject_user_id"], ["users.id"], ondelete="SET NULL"),
    )
    for name, cols in (
        ("ix_affiliate_audit_events_event_type", ["event_type"]),
        ("ix_affiliate_audit_events_actor_user_id", ["actor_user_id"]),
        ("ix_affiliate_audit_events_partner_id", ["partner_id"]),
        ("ix_affiliate_audit_events_subject_user_id", ["subject_user_id"]),
        ("ix_affiliate_audit_events_source_ref", ["source_ref"]),
        ("ix_affiliate_audit_events_created_at", ["created_at"]),
    ):
        op.create_index(name, "affiliate_audit_events", cols)

    # Draft by design. Production cannot accept partners or move money until an
    # administrator explicitly publishes the reviewed legal copy.
    op.bulk_insert(
        sa.table(
            "affiliate_program_terms",
            sa.column("version", sa.String()),
            sa.column("status", sa.String()),
            sa.column("commission_rate_bps", sa.Integer()),
            sa.column("commission_months", sa.Integer()),
            sa.column("attribution_window_days", sa.Integer()),
            sa.column("payout_minimum_minor", sa.BigInteger()),
            sa.column("hold_days", sa.Integer()),
            sa.column("currency", sa.String()),
            sa.column("commission_basis", sa.String()),
            sa.column("legal_text", sa.Text()),
            sa.column("legal_copy_checksum", sa.String()),
        ),
        [
            {
                "version": "v1",
                "status": "draft",
                "commission_rate_bps": 3000,
                "commission_months": 12,
                "attribution_window_days": 60,
                "payout_minimum_minor": 5000,
                "hold_days": 30,
                "currency": "usd",
                "commission_basis": "invoice_amount_paid_excluding_tax",
                "legal_text": V1_LEGAL_TEXT,
                "legal_copy_checksum": V1_LEGAL_CHECKSUM,
            }
        ],
        multiinsert=False,
    )


def downgrade() -> None:
    for table in (
        "affiliate_audit_events",
        "affiliate_payout_items",
        "affiliate_payouts",
        "affiliate_commission_states",
        "affiliate_commission_entries",
        "affiliate_attributions",
        "affiliate_clicks",
        "affiliate_terms_acceptances",
        "affiliate_partners",
        "affiliate_applications",
        "affiliate_program_terms",
    ):
        op.drop_table(table)
    op.drop_table("referral_invite_deliveries")
    op.drop_index("ix_referrals_reward_source_invoice_id", table_name="referrals")
    op.drop_column("referrals", "reward_reversed_at")
    op.drop_column("referrals", "reward_source_invoice_id")
