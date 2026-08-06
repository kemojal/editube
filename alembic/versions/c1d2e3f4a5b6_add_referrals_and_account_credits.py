"""add referral codes, referrals and the account credit ledger

The referral program existed only as a marketing page (/partners/affiliate) with
no data behind it, so nothing could be shown in Settings and no reward could be
paid. This adds the three tables the program needs:

  referral_codes         one share link per user, with its guest-pass allowance
  referrals              one row per friend who signed up on a link
  account_credit_ledger  append-only AI credits; referral rewards pay into it

The ledger is deliberately account-scoped and separate from aiugc.ugc_credit_ledger
(which meters one workspace's ad spend) so neither program can drain the other.

Revision ID: c1d2e3f4a5b6
Revises: f1a2b3c4d5e6
Create Date: 2026-08-07
"""

from alembic import op
import sqlalchemy as sa


revision = "c1d2e3f4a5b6"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "referral_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("passes_total", sa.Integer(), server_default="3", nullable=False),
        sa.Column("revoked_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
        sa.UniqueConstraint("code"),
    )
    op.create_index("ix_referral_codes_user_id", "referral_codes", ["user_id"])
    op.create_index("ix_referral_codes_code", "referral_codes", ["code"])

    op.create_table(
        "referrals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("referrer_user_id", sa.Integer(), nullable=False),
        sa.Column("referral_code_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("invitee_user_id", sa.Integer(), nullable=False),
        sa.Column("invitee_email", sa.String(), nullable=True),
        sa.Column("status", sa.String(), server_default="signed_up", nullable=False),
        sa.Column("void_reason", sa.String(), nullable=True),
        sa.Column("pass_trial_days", sa.Integer(), server_default="30", nullable=False),
        sa.Column("pass_redeemed_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("signed_up_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("converted_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("rewarded_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("reward_credits", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["referrer_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["invitee_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["referral_code_id"], ["referral_codes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # One referral per referred account, ever — re-signup cannot mint a
        # second reward for the same person.
        sa.UniqueConstraint("invitee_user_id", name="uq_referrals_invitee_user"),
    )
    op.create_index("ix_referrals_referrer_user_id", "referrals", ["referrer_user_id"])
    op.create_index("ix_referrals_referral_code_id", "referrals", ["referral_code_id"])
    op.create_index("ix_referrals_invitee_user_id", "referrals", ["invitee_user_id"])
    op.create_index("ix_referrals_status", "referrals", ["status"])

    op.create_table(
        "account_credit_ledger",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("source_ref", sa.String(), nullable=True),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # Idempotency: a webhook that fires twice must not pay twice.
        sa.UniqueConstraint("user_id", "reason", "source_ref", name="uq_account_credit_source"),
    )
    op.create_index("ix_account_credit_ledger_user_id", "account_credit_ledger", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_account_credit_ledger_user_id", table_name="account_credit_ledger")
    op.drop_table("account_credit_ledger")

    op.drop_index("ix_referrals_status", table_name="referrals")
    op.drop_index("ix_referrals_invitee_user_id", table_name="referrals")
    op.drop_index("ix_referrals_referral_code_id", table_name="referrals")
    op.drop_index("ix_referrals_referrer_user_id", table_name="referrals")
    op.drop_table("referrals")

    op.drop_index("ix_referral_codes_code", table_name="referral_codes")
    op.drop_index("ix_referral_codes_user_id", table_name="referral_codes")
    op.drop_table("referral_codes")
