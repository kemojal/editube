"""Add saved transcript highlights/comments to repurpose clips.

Revision ID: d4e5f6a7b8c9
Revises: c2d3e4f5a6b7
Create Date: 2026-04-24
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "d4e5f6a7b8c9"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "clips",
        sa.Column(
            "transcript_highlights",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        schema="repurpose",
    )
    op.add_column(
        "clips",
        sa.Column(
            "transcript_comments",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        schema="repurpose",
    )


def downgrade() -> None:
    op.drop_column("clips", "transcript_comments", schema="repurpose")
    op.drop_column("clips", "transcript_highlights", schema="repurpose")
