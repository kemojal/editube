"""add duration to annotations, change timecode to integer

Revision ID: d6e7f8a9b0c1
Revises: f0a1b2c3d4e6
Create Date: 2026-04-10 20:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "d6e7f8a9b0c1"
down_revision = "f0a1b2c3d4e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # timecode was numrange/varchar — drop and recreate as integer
    op.drop_column("annotations", "timecode")
    op.add_column(
        "annotations",
        sa.Column("timecode", sa.Integer(), nullable=False, server_default="0"),
    )
    # Add duration column
    op.add_column(
        "annotations",
        sa.Column("duration", sa.Integer(), server_default="5", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("annotations", "duration")
    op.drop_column("annotations", "timecode")
    op.add_column(
        "annotations",
        sa.Column("timecode", sa.String(), nullable=False, server_default="0"),
    )
