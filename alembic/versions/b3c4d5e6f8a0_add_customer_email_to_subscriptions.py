"""add customer_email snapshot to subscriptions

Revision ID: b3c4d5e6f8a0
Revises: a1b2c3d4e5f7
Create Date: 2026-04-10 16:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "b3c4d5e6f8a0"
down_revision = "a1b2c3d4e5f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("subscriptions", sa.Column("customer_email", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("subscriptions", "customer_email")
