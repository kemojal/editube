"""add user_settings.ai_model_preferences

Revision ID: f8a9b1c2d3e4
Revises: e7f8a9b1c2d3
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "f8a9b1c2d3e4"
down_revision = "e7f8a9b1c2d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("ai_model_preferences", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "ai_model_preferences")
