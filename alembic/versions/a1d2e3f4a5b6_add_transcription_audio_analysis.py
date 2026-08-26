"""add audio_analysis (VAD speech/silence ranges) to video_transcriptions

Revision ID: a1d2e3f4a5b6
Revises: f3a1b2c4d5e6
Create Date: 2026-08-12
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "a1d2e3f4a5b6"
down_revision = "f3a1b2c4d5e6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("video_transcriptions", sa.Column("audio_analysis", JSONB(), nullable=True))


def downgrade():
    op.drop_column("video_transcriptions", "audio_analysis")
