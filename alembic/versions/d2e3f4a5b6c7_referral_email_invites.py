"""let a referral exist before the friend does (email invites)

A referral row used to require an account: `invitee_user_id` was NOT NULL, so
nothing could be recorded between "link copied" and "someone signed up". Sending
an invite by email needs a row that exists with only an address on it.

Rather than add a parallel `referral_invites` table, the existing row is relaxed
to allow a pending state. Pass accounting, the reward path and the settings list
then work unchanged for both kinds of referral.

  invited ──accepted──> signed_up ──> trialing ──> rewarded
     │                                    └──> void
     └──expired (pass returned)

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-08-07
"""

from alembic import op
import sqlalchemy as sa


revision = "d2e3f4a5b6c7"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A pending invite has no account behind it yet. Postgres allows any number
    # of NULLs under a unique constraint, so uq_referrals_invitee_user keeps
    # doing its job for rows that *do* have a user.
    op.alter_column("referrals", "invitee_user_id", existing_type=sa.Integer(), nullable=True)

    # `signed_up_at` defaulted to now(), which would stamp an invite with a
    # signup date it hasn't got. It is set explicitly on acceptance instead.
    op.alter_column(
        "referrals",
        "signed_up_at",
        existing_type=sa.TIMESTAMP(),
        nullable=True,
        server_default=None,
    )

    op.add_column("referrals", sa.Column("invited_at", sa.TIMESTAMP(), nullable=True))
    op.add_column("referrals", sa.Column("invite_expires_at", sa.TIMESTAMP(), nullable=True))
    op.add_column("referrals", sa.Column("invite_last_sent_at", sa.TIMESTAMP(), nullable=True))
    op.add_column(
        "referrals", sa.Column("invite_sends", sa.Integer(), server_default="0", nullable=False)
    )

    # One live invite per address per referrer. Partial so that a expired or
    # accepted invite doesn't block inviting the same person again later, and
    # case-insensitive because nobody types their friend's address twice the
    # same way.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_referrals_pending_invite
            ON referrals (referrer_user_id, lower(invitee_email))
         WHERE status = 'invited'
        """
    )
    op.execute("CREATE INDEX ix_referrals_invitee_email ON referrals (lower(invitee_email))")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_referrals_invitee_email")
    op.execute("DROP INDEX IF EXISTS uq_referrals_pending_invite")

    op.drop_column("referrals", "invite_sends")
    op.drop_column("referrals", "invite_last_sent_at")
    op.drop_column("referrals", "invite_expires_at")
    op.drop_column("referrals", "invited_at")

    # Rows that never had a signup can't be represented once the column is
    # mandatory again, so drop the pending ones on the way down.
    op.execute("DELETE FROM referrals WHERE invitee_user_id IS NULL")
    op.execute("UPDATE referrals SET signed_up_at = created_at WHERE signed_up_at IS NULL")
    op.alter_column(
        "referrals",
        "signed_up_at",
        existing_type=sa.TIMESTAMP(),
        nullable=False,
        server_default=sa.text("now()"),
    )
    op.alter_column("referrals", "invitee_user_id", existing_type=sa.Integer(), nullable=False)
