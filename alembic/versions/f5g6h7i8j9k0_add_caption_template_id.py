"""add caption_template_id to clip_styles

Revision ID: f5g6h7i8j9k0
Revises: e4f5g6h7i8j9
Create Date: 2026-04-28 00:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "f5g6h7i8j9k0"
down_revision = "e4f5g6h7i8j9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "clip_styles",
        sa.Column("caption_template_id", sa.String(), nullable=True),
        schema="repurpose",
    )


def downgrade() -> None:
    op.drop_column("clip_styles", "caption_template_id", schema="repurpose")
