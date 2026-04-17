"""annotation timecode and duration as float for sub-second precision

Revision ID: a1b2c3e4f5g6
Revises: z3a4b5c6d7e8
Create Date: 2026-04-17 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "a1b2c3e4f5g6"
down_revision = "z3a4b5c6d7e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "annotations",
        "timecode",
        type_=sa.Float(),
        existing_nullable=False,
        postgresql_using="timecode::double precision",
    )
    op.alter_column(
        "annotations",
        "duration",
        type_=sa.Float(),
        existing_nullable=False,
        existing_server_default="5",
        postgresql_using="duration::double precision",
    )


def downgrade() -> None:
    op.alter_column(
        "annotations",
        "timecode",
        type_=sa.Integer(),
        existing_nullable=False,
        postgresql_using="timecode::integer",
    )
    op.alter_column(
        "annotations",
        "duration",
        type_=sa.Integer(),
        existing_nullable=False,
        postgresql_using="duration::integer",
    )
