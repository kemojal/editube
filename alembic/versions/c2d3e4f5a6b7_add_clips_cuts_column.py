"""Add clips.cuts JSONB for multi-range text-based editing.

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-04-23

Stores the list of kept source-time ranges for a clip so transcript-based
deletions produce concatenated renders. Existing rows are backfilled with
a single range matching their current start_time/end_time so behavior is
unchanged until the user edits.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "c2d3e4f5a6b7"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "clips",
        sa.Column(
            "cuts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        schema="repurpose",
    )
    op.execute(
        """
        UPDATE repurpose.clips
        SET cuts = jsonb_build_array(
            jsonb_build_object('start', start_time, 'end', end_time)
        )
        WHERE cuts IS NULL OR cuts = '[]'::jsonb
        """
    )


def downgrade() -> None:
    op.drop_column("clips", "cuts", schema="repurpose")
