"""add clip edit history

Revision ID: e4f5g6h7i8j9
Revises: d4e5f6a7b8c9
Create Date: 2026-04-24 16:40:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "e4f5g6h7i8j9"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "clips",
        sa.Column(
            "edit_history",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        schema="repurpose",
    )


def downgrade() -> None:
    op.drop_column("clips", "edit_history", schema="repurpose")
