"""add user_zoom_connections table

Revision ID: a9b1c2d3e4f5
Revises: f8a9b1c2d3e4
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa


revision = "a9b1c2d3e4f5"
down_revision = "f8a9b1c2d3e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_zoom_connections",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("zoom_user_id", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=False),
        sa.Column("access_token", sa.Text(), nullable=True),
        sa.Column("access_expires_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_user_zoom_connections_user_id", "user_zoom_connections", ["user_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_user_zoom_connections_user_id", table_name="user_zoom_connections")
    op.drop_table("user_zoom_connections")
