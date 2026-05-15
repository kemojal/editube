"""user caption favorites

Revision ID: b6c7d8e9f0a1
Revises: a1b2c3d4e5f6
Create Date: 2026-05-15 11:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "b6c7d8e9f0a1"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_caption_favorites",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("template_id", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "template_id", name="uq_user_caption_favorites_user_template"),
    )
    op.create_index(
        op.f("ix_user_caption_favorites_id"),
        "user_caption_favorites",
        ["id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_user_caption_favorites_user_id"),
        "user_caption_favorites",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_user_caption_favorites_user_id"), table_name="user_caption_favorites")
    op.drop_index(op.f("ix_user_caption_favorites_id"), table_name="user_caption_favorites")
    op.drop_table("user_caption_favorites")
