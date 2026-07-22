"""add language and detected_language to video_transcriptions

Revision ID: c4d5e6f7a8b9
Revises: aiugc_0001
Create Date: 2026-07-21
"""

from alembic import op
import sqlalchemy as sa

revision = "c4d5e6f7a8b9"
down_revision = "aiugc_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("video_transcriptions", sa.Column("language", sa.String(), nullable=True))
    op.add_column("video_transcriptions", sa.Column("detected_language", sa.String(), nullable=True))


def downgrade():
    op.drop_column("video_transcriptions", "detected_language")
    op.drop_column("video_transcriptions", "language")
