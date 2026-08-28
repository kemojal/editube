"""add privacy-safe video ingest source

Revision ID: ag2908290001
Revises: lg2908290001
Create Date: 2026-08-29
"""

from alembic import op
import sqlalchemy as sa


revision = "ag2908290001"
down_revision = "lg2908290001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("videos", sa.Column("ingest_source", sa.String(), nullable=True))
    op.create_index("ix_videos_ingest_source", "videos", ["ingest_source"])
    # Existing watch-folder uploads embedded a sensitive local path in the
    # description. Retain origin attribution while removing that path.
    op.execute(
        """
        UPDATE videos
        SET ingest_source = 'watch_folder',
            description = 'Auto-uploaded from watch folder'
        WHERE description LIKE 'Auto-uploaded from watch folder:%'
        """
    )


def downgrade() -> None:
    op.drop_index("ix_videos_ingest_source", table_name="videos")
    op.drop_column("videos", "ingest_source")
