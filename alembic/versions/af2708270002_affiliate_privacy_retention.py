"""affiliate click and acceptance privacy retention marker

Revision ID: af2708270002
Revises: af2708270001
Create Date: 2026-08-27
"""

from alembic import op
import sqlalchemy as sa


revision = "af2708270002"
down_revision = "af2708270001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "affiliate_clicks",
        sa.Column("privacy_scrubbed_at", sa.TIMESTAMP(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("affiliate_clicks", "privacy_scrubbed_at")
