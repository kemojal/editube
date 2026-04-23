"""Store original YouTube page URL on videos for reliable transcription audio resolve.

Revision ID: b1c2d3e4f5a6
Revises: a0b1c2d3e4f5
Create Date: 2026-04-23
"""

from alembic import op
import sqlalchemy as sa


revision = "b1c2d3e4f5a6"
down_revision = "a0b1c2d3e4f5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "videos",
        sa.Column("ingest_page_url", sa.Text(), nullable=True),
    )
    # Backfill from repurpose jobs so existing YouTube sources can re-transcribe without re-import.
    op.execute(
        """
        UPDATE videos v
        SET ingest_page_url = j.source_url
        FROM repurpose.repurpose_jobs j
        WHERE j.video_id = v.id
          AND j.source_mode = 'youtube_url'
          AND j.source_url IS NOT NULL
          AND btrim(j.source_url) <> ''
          AND v.ingest_page_url IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("videos", "ingest_page_url")
