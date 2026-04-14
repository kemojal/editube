"""add video status/duration, comment threading/likes/drawings

Revision ID: c5d6e7f8a9b0
Revises: b3c4d5e6f8a0
Create Date: 2026-04-10 18:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "c5d6e7f8a9b0"
down_revision = "b3c4d5e6f8a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Video: add status and duration columns
    op.add_column("videos", sa.Column("status", sa.String(), server_default="in_progress", nullable=False))
    op.add_column("videos", sa.Column("duration", sa.Integer(), nullable=True))

    # Comment: add parent_id for threading, end_timecode for ranges, drawing_data, and is_resolved
    op.add_column("comments", sa.Column("parent_id", sa.Integer(), sa.ForeignKey("comments.id"), nullable=True))
    op.add_column("comments", sa.Column("end_timecode", sa.Integer(), nullable=True))
    op.add_column("comments", sa.Column("drawing_data", JSONB(), nullable=True))
    op.add_column("comments", sa.Column("is_resolved", sa.Boolean(), server_default="false", nullable=False))

    # CommentLike table
    op.create_table(
        "comment_likes",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("comment_id", sa.Integer(), sa.ForeignKey("comments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now()),
    )
    op.create_index("ix_comment_likes_unique", "comment_likes", ["comment_id", "user_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_comment_likes_unique", table_name="comment_likes")
    op.drop_table("comment_likes")
    op.drop_column("comments", "is_resolved")
    op.drop_column("comments", "drawing_data")
    op.drop_column("comments", "end_timecode")
    op.drop_column("comments", "parent_id")
    op.drop_column("videos", "duration")
    op.drop_column("videos", "status")
