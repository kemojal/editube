"""add user_youtube_connections for YouTube Data API OAuth

Revision ID: z2b3c4d5e6f7
Revises: y7z8a9b0c1d2
Create Date: 2026-04-15
"""

from alembic import op
import sqlalchemy as sa

revision = "z2b3c4d5e6f7"
down_revision = "y7z8a9b0c1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_youtube_connections",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.String(), nullable=True),
        sa.Column("channel_title", sa.String(), nullable=True),
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=False),
        sa.Column("access_token", sa.Text(), nullable=True),
        sa.Column("access_expires_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("scopes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_user_youtube_connections_user_id"),
    )
    op.create_index(
        "ix_user_youtube_connections_user_id",
        "user_youtube_connections",
        ["user_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_user_youtube_connections_user_id", table_name="user_youtube_connections")
    op.drop_table("user_youtube_connections")
