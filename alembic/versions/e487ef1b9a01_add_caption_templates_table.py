"""add caption_templates table

Editable caption-template catalogue for the rough-cut editor. Previously the
templates only existed as frontend constants; this table lets internal users
polish, create and archive templates from the editor itself.

Revision ID: e487ef1b9a01
Revises: d8e9f0a1b2c3
Create Date: 2026-08-17
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "e487ef1b9a01"
down_revision = "d8e9f0a1b2c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "caption_templates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("category", sa.String(length=24), server_default="new", nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("sample", sa.String(length=200), server_default="", nullable=False),
        sa.Column("tag", sa.String(length=40), nullable=True),
        sa.Column("blurb", sa.Text(), server_default="", nullable=False),
        sa.Column("patch", JSONB(), server_default="{}", nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("archived", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("builtin", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_caption_templates_slug", "caption_templates", ["slug"], unique=True)
    op.create_index("ix_caption_templates_sort_order", "caption_templates", ["sort_order"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_caption_templates_sort_order", table_name="caption_templates")
    op.drop_index("ix_caption_templates_slug", table_name="caption_templates")
    op.drop_table("caption_templates")
