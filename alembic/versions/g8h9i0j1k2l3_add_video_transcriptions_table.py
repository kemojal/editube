"""add video_transcriptions table

Revision ID: g8h9i0j1k2l3
Revises: d6e7f8a9b0c1
Create Date: 2026-04-14

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "g8h9i0j1k2l3"
down_revision = "d6e7f8a9b0c1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "video_transcriptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("video_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column("segments", JSONB(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("model_name", sa.String(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=True),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("video_id", name="uq_video_transcriptions_video_id"),
    )
    op.create_index(
        op.f("ix_video_transcriptions_video_id"),
        "video_transcriptions",
        ["video_id"],
        unique=False,
    )


def downgrade():
    op.drop_index(op.f("ix_video_transcriptions_video_id"), table_name="video_transcriptions")
    op.drop_table("video_transcriptions")
