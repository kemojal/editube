"""add comments revision column

Revision ID: r1s2t3u4v5w6
Revises: 3ea23652a7e1
Create Date: 2026-04-15 19:05:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "r1s2t3u4v5w6"
down_revision = "3ea23652a7e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("comments")}
    if "revision" not in columns:
        op.add_column(
            "comments",
            sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("comments")}
    if "revision" in columns:
        op.drop_column("comments", "revision")
