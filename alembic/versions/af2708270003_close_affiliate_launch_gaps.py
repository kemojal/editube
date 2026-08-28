"""close affiliate launch and referral operations gaps

Revision ID: af2708270003
Revises: mp2708270001
Create Date: 2026-08-27
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "af2708270003"
down_revision = "mp2708270001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("referrals", sa.Column("reward_dispute_active", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("referrals", sa.Column("reward_reinstated_at", sa.TIMESTAMP(), nullable=True))
    op.add_column("referrals", sa.Column("capacity_released_at", sa.TIMESTAMP(), nullable=True))
    op.add_column("referrals", sa.Column("capacity_release_reason", sa.String(), nullable=True))

    op.drop_constraint("ck_referral_delivery_status", "referral_invite_deliveries", type_="check")
    op.add_column("referral_invite_deliveries", sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False))
    op.add_column("referral_invite_deliveries", sa.Column("next_retry_at", sa.TIMESTAMP(), nullable=True))
    op.add_column("referral_invite_deliveries", sa.Column("last_attempt_at", sa.TIMESTAMP(), nullable=True))
    op.add_column("referral_invite_deliveries", sa.Column("suppression_reason", sa.String(), nullable=True))
    op.create_check_constraint(
        "ck_referral_delivery_status",
        "referral_invite_deliveries",
        "status IN ('queued','sent','failed','suppressed')",
    )
    op.create_index(
        "ix_referral_invite_deliveries_next_retry_at",
        "referral_invite_deliveries",
        ["next_retry_at"],
    )

    op.create_table(
        "referral_email_suppressions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email_hash", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("provider_event_id", sa.String(), nullable=True),
        sa.Column("suppressed_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("cleared_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("cleared_by_user_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["cleared_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("email_hash"),
        sa.UniqueConstraint("provider_event_id"),
    )
    for column in ("id", "email_hash", "provider_event_id"):
        op.create_index(f"ix_referral_email_suppressions_{column}", "referral_email_suppressions", [column])

    op.create_table(
        "referral_email_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider_event_id", sa.String(), nullable=False),
        sa.Column("email_hash", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("occurred_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("provider_event_id"),
    )
    for column in ("id", "provider_event_id", "email_hash", "event_type"):
        op.create_index(f"ix_referral_email_events_{column}", "referral_email_events", [column])

    op.create_table(
        "referral_admin_audit_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("subject_user_id", sa.Integer(), nullable=True),
        sa.Column("source_ref", sa.String(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["subject_user_id"], ["users.id"], ondelete="SET NULL"),
    )
    for column in ("id", "event_type", "actor_user_id", "subject_user_id", "source_ref", "created_at"):
        op.create_index(f"ix_referral_admin_audit_events_{column}", "referral_admin_audit_events", [column])

    op.create_table(
        "affiliate_campaigns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("partner_id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("destination_path", sa.String(), server_default="/", nullable=False),
        sa.Column("status", sa.String(), server_default="active", nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["partner_id"], ["affiliate_partners.id"], ondelete="CASCADE"),
        sa.CheckConstraint("status IN ('active','paused','archived')", name="ck_affiliate_campaign_status"),
        sa.UniqueConstraint("partner_id", "slug", name="uq_affiliate_campaign_partner_slug"),
    )
    for column in ("id", "partner_id", "slug", "status"):
        op.create_index(f"ix_affiliate_campaigns_{column}", "affiliate_campaigns", [column])
    op.add_column("affiliate_clicks", sa.Column("campaign_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_affiliate_clicks_campaign_id",
        "affiliate_clicks",
        "affiliate_campaigns",
        ["campaign_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_affiliate_clicks_campaign_id", "affiliate_clicks", ["campaign_id"])

    op.add_column(
        "affiliate_attributions",
        sa.Column("risk_flags", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    op.create_table(
        "affiliate_compliance_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("partner_id", sa.Integer(), nullable=False),
        sa.Column("tax_residency_country", sa.String(), nullable=True),
        sa.Column("tax_form_type", sa.String(), nullable=True),
        sa.Column("tax_form_reference_hash", sa.String(), nullable=True),
        sa.Column("tax_verified_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("tax_verified_by_user_id", sa.Integer(), nullable=True),
        sa.Column("sanctions_status", sa.String(), server_default="pending", nullable=False),
        sa.Column("sanctions_checked_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("sanctions_checked_by_user_id", sa.Integer(), nullable=True),
        sa.Column("withholding_rate_bps", sa.Integer(), server_default="0", nullable=False),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["partner_id"], ["affiliate_partners.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tax_verified_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["sanctions_checked_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("sanctions_status IN ('pending','clear','review','blocked')", name="ck_affiliate_compliance_sanctions"),
        sa.CheckConstraint("withholding_rate_bps BETWEEN 0 AND 10000", name="ck_affiliate_compliance_withholding"),
        sa.UniqueConstraint("partner_id"),
    )
    for column in ("id", "partner_id", "sanctions_status"):
        op.create_index(f"ix_affiliate_compliance_profiles_{column}", "affiliate_compliance_profiles", [column])

    op.create_table(
        "affiliate_launch_approvals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("terms_version_id", sa.Integer(), nullable=False),
        sa.Column("approval_role", sa.String(), nullable=False),
        sa.Column("approved_by_user_id", sa.Integer(), nullable=False),
        sa.Column("terms_checksum", sa.String(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("approved_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("revoked_by_user_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["terms_version_id"], ["affiliate_program_terms.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["approved_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["revoked_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("approval_role IN ('legal','finance','product','engineering')", name="ck_affiliate_launch_approval_role"),
    )
    for column in ("id", "terms_version_id", "approval_role", "approved_by_user_id"):
        op.create_index(f"ix_affiliate_launch_approvals_{column}", "affiliate_launch_approvals", [column])
    op.create_index(
        "uq_affiliate_launch_approval_active_role",
        "affiliate_launch_approvals",
        ["terms_version_id", "approval_role"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.add_column("affiliate_payouts", sa.Column("gross_amount_minor", sa.BigInteger(), server_default="0", nullable=False))
    op.add_column("affiliate_payouts", sa.Column("withholding_rate_bps", sa.Integer(), server_default="0", nullable=False))
    op.add_column("affiliate_payouts", sa.Column("withholding_minor", sa.BigInteger(), server_default="0", nullable=False))
    op.execute("UPDATE affiliate_payouts SET gross_amount_minor = amount_minor")
    op.create_check_constraint("ck_affiliate_payout_gross_positive", "affiliate_payouts", "gross_amount_minor > 0")
    op.create_check_constraint("ck_affiliate_payout_withholding_rate", "affiliate_payouts", "withholding_rate_bps BETWEEN 0 AND 10000")
    op.create_check_constraint("ck_affiliate_payout_withholding_amount", "affiliate_payouts", "withholding_minor >= 0 AND withholding_minor <= gross_amount_minor")
    op.create_check_constraint("ck_affiliate_payout_net_positive", "affiliate_payouts", "amount_minor > 0 AND amount_minor = gross_amount_minor - withholding_minor")


def downgrade() -> None:
    for name in (
        "ck_affiliate_payout_net_positive",
        "ck_affiliate_payout_withholding_amount",
        "ck_affiliate_payout_withholding_rate",
        "ck_affiliate_payout_gross_positive",
    ):
        op.drop_constraint(name, "affiliate_payouts", type_="check")
    op.drop_column("affiliate_payouts", "withholding_minor")
    op.drop_column("affiliate_payouts", "withholding_rate_bps")
    op.drop_column("affiliate_payouts", "gross_amount_minor")

    op.drop_index("uq_affiliate_launch_approval_active_role", table_name="affiliate_launch_approvals")
    op.drop_table("affiliate_launch_approvals")
    op.drop_table("affiliate_compliance_profiles")
    op.drop_column("affiliate_attributions", "risk_flags")
    op.drop_index("ix_affiliate_clicks_campaign_id", table_name="affiliate_clicks")
    op.drop_constraint("fk_affiliate_clicks_campaign_id", "affiliate_clicks", type_="foreignkey")
    op.drop_column("affiliate_clicks", "campaign_id")
    op.drop_table("affiliate_campaigns")

    op.drop_table("referral_admin_audit_events")
    op.drop_table("referral_email_events")
    op.drop_table("referral_email_suppressions")
    op.drop_index("ix_referral_invite_deliveries_next_retry_at", table_name="referral_invite_deliveries")
    op.drop_constraint("ck_referral_delivery_status", "referral_invite_deliveries", type_="check")
    op.drop_column("referral_invite_deliveries", "suppression_reason")
    op.drop_column("referral_invite_deliveries", "last_attempt_at")
    op.drop_column("referral_invite_deliveries", "next_retry_at")
    op.drop_column("referral_invite_deliveries", "retry_count")
    op.create_check_constraint(
        "ck_referral_delivery_status",
        "referral_invite_deliveries",
        "status IN ('queued','sent','failed')",
    )
    op.drop_column("referrals", "capacity_release_reason")
    op.drop_column("referrals", "capacity_released_at")
    op.drop_column("referrals", "reward_reinstated_at")
    op.drop_column("referrals", "reward_dispute_active")
