"""add speakers fields to video_transcriptions

Revision ID: s9t0u1v2w3x4
Revises: r8s9t0u1v2w3
Create Date: 2026-04-14
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "s9t0u1v2w3x4"
down_revision = "r8s9t0u1v2w3"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("video_transcriptions", sa.Column("speakers", JSONB(), nullable=True))
    op.add_column("video_transcriptions", sa.Column("speaker_count", sa.Integer(), nullable=True))


def downgrade():
    op.drop_column("video_transcriptions", "speaker_count")
    op.drop_column("video_transcriptions", "speakers")
