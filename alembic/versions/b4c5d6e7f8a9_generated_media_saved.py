"""generated_media.saved — review before adding to the media panel

Revision ID: b4c5d6e7f8a9
Revises: e6f7a8b9c0d1
Create Date: 2026-07-28
"""

from alembic import op
import sqlalchemy as sa


revision = "b4c5d6e7f8a9"
down_revision = "e6f7a8b9c0d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "generated_media",
        sa.Column("saved", sa.Boolean(), server_default="false", nullable=False),
    )
    op.create_index("ix_generated_media_saved", "generated_media", ["saved"])
    # Anything generated before review existed was auto-added, so keep it visible.
    op.execute("UPDATE generated_media SET saved = true WHERE status = 'ready'")


def downgrade() -> None:
    op.drop_index("ix_generated_media_saved", table_name="generated_media")
    op.drop_column("generated_media", "saved")
