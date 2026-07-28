"""generated_media.reference_urls — image-seeded generation

Revision ID: c5d6e7f8a9b1
Revises: b4c5d6e7f8a9
Create Date: 2026-07-28
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "c5d6e7f8a9b1"
down_revision = "b4c5d6e7f8a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "generated_media",
        sa.Column(
            "reference_urls",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("generated_media", "reference_urls")
