"""add user_settings.share_data + default_publish_privacy and api_tokens table

Revision ID: d6e7f8a9b1c2
Revises: c5d6e7f8a9b1
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa


revision = "d6e7f8a9b1c2"
down_revision = "c5d6e7f8a9b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("share_data", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "user_settings",
        sa.Column(
            "default_publish_privacy",
            sa.String(),
            nullable=False,
            server_default="private",
        ),
    )

    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("token_prefix", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("last_used_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_api_tokens_user_id", "api_tokens", ["user_id"], unique=False)
    op.create_index("ix_api_tokens_token_hash", "api_tokens", ["token_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_api_tokens_token_hash", table_name="api_tokens")
    op.drop_index("ix_api_tokens_user_id", table_name="api_tokens")
    op.drop_table("api_tokens")
    op.drop_column("user_settings", "default_publish_privacy")
    op.drop_column("user_settings", "share_data")
