"""add comments.end_timecode if missing (repair partial c5d6e7f8a9b0)

Revision ID: e8f9a0b1c2d3
Revises: c5d6e7f8a9b0
Create Date: 2026-04-10

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "e8f9a0b1c2d3"
down_revision = "c5d6e7f8a9b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    cols = {c["name"] for c in inspect(conn).get_columns("comments")}
    if "end_timecode" not in cols:
        op.add_column("comments", sa.Column("end_timecode", sa.Integer(), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    cols = {c["name"] for c in inspect(conn).get_columns("comments")}
    if "end_timecode" in cols:
        op.drop_column("comments", "end_timecode")
