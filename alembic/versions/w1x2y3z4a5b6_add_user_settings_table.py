"""add user settings table

Revision ID: w1x2y3z4a5b6
Revises: v3w4x5y6z7a8
Create Date: 2026-04-14
"""

from alembic import op
import sqlalchemy as sa


revision = "w1x2y3z4a5b6"
down_revision = "v3w4x5y6z7a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_settings",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("timezone", sa.String(), nullable=False, server_default="America/Los_Angeles"),
        sa.Column("theme", sa.String(), nullable=False, server_default="system"),
        sa.Column("date_format", sa.String(), nullable=False, server_default="MMM d, yyyy"),
        sa.Column("email_comments", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("email_mentions", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("product_updates", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("two_factor", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("session_timeout", sa.String(), nullable=False, server_default="30"),
        sa.Column("allow_project_invites", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_user_settings_user_id", "user_settings", ["user_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_user_settings_user_id", table_name="user_settings")
    op.drop_table("user_settings")
