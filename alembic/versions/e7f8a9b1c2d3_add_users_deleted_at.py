"""add users.deleted_at for account soft-deletion

Revision ID: e7f8a9b1c2d3
Revises: d6e7f8a9b1c2
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa


revision = "e7f8a9b1c2d3"
down_revision = "d6e7f8a9b1c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("deleted_at", sa.TIMESTAMP(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "deleted_at")
