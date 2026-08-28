"""record first dashboard view atomically

Revision ID: ag2908290002
Revises: ag2908290001
Create Date: 2026-08-29
"""

from alembic import op
import sqlalchemy as sa


revision = "ag2908290002"
down_revision = "ag2908290001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("first_dashboard_viewed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "first_dashboard_viewed_at")
