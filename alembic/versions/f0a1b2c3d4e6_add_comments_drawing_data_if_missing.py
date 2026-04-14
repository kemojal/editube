"""add comments.drawing_data if missing (repair partial c5d6e7f8a9b0)

Revision ID: f0a1b2c3d4e6
Revises: e8f9a0b1c2d3
Create Date: 2026-04-10

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB

revision = "f0a1b2c3d4e6"
down_revision = "e8f9a0b1c2d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    cols = {c["name"] for c in inspect(conn).get_columns("comments")}
    if "drawing_data" not in cols:
        op.add_column("comments", sa.Column("drawing_data", JSONB(), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    cols = {c["name"] for c in inspect(conn).get_columns("comments")}
    if "drawing_data" in cols:
        op.drop_column("comments", "drawing_data")
