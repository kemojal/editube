"""persist checkout attempts for mature abandonment

Revision ID: ag2908290003
Revises: ag2908290002
Create Date: 2026-08-29
"""

from alembic import op
import sqlalchemy as sa


revision = "ag2908290003"
down_revision = "ag2908290002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "checkout_attempts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("workspace_id", sa.Integer(), nullable=True),
        sa.Column("stripe_checkout_session_id", sa.String(), nullable=False),
        sa.Column("plan", sa.String(), nullable=False),
        sa.Column("recurring_interval", sa.String(), nullable=False),
        sa.Column("campaign_id", sa.String(), nullable=True),
        sa.Column("source", sa.String(), server_default="billing_checkout", nullable=False),
        sa.Column("trial_days", sa.Integer(), server_default="0", nullable=False),
        sa.Column("offer_applied", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("status", sa.String(), server_default="created", nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("canceled_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("abandoned_at", sa.TIMESTAMP(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stripe_checkout_session_id"),
    )
    for name in ("id", "user_id", "workspace_id", "stripe_checkout_session_id", "status", "created_at"):
        op.create_index(f"ix_checkout_attempts_{name}", "checkout_attempts", [name])


def downgrade() -> None:
    op.drop_table("checkout_attempts")
