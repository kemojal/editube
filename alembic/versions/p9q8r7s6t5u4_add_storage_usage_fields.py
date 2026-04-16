"""add storage usage fields

Revision ID: p9q8r7s6t5u4
Revises: a7b8c9d0e1f2, s1t2u3v4w5x6
Create Date: 2026-04-16 12:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "p9q8r7s6t5u4"
down_revision = ("a7b8c9d0e1f2", "s1t2u3v4w5x6")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("storage_grace_until", sa.TIMESTAMP(), nullable=True))
    op.add_column("videos", sa.Column("size_bytes", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    op.drop_column("videos", "size_bytes")
    op.drop_column("users", "storage_grace_until")
