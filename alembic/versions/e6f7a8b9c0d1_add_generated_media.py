"""add generated_media

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-07-28

AI-generated images and videos, stored as project assets. The row is created
before generation starts so the media panel can show a pending tile with live
progress; the worker advances status and fills in the storage URL.
"""

from alembic import op
import sqlalchemy as sa


revision = "e6f7a8b9c0d1"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "generated_media",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("video_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("aspect_ratio", sa.String(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column("progress", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("thumbnail_url", sa.Text(), nullable=True),
        sa.Column("storage_key", sa.Text(), nullable=True),
        sa.Column("mime_type", sa.String(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_generated_media_id", "generated_media", ["id"])
    op.create_index("ix_generated_media_project_id", "generated_media", ["project_id"])
    op.create_index("ix_generated_media_video_id", "generated_media", ["video_id"])
    op.create_index("ix_generated_media_user_id", "generated_media", ["user_id"])
    op.create_index("ix_generated_media_kind", "generated_media", ["kind"])
    op.create_index("ix_generated_media_status", "generated_media", ["status"])


def downgrade() -> None:
    op.drop_index("ix_generated_media_status", table_name="generated_media")
    op.drop_index("ix_generated_media_kind", table_name="generated_media")
    op.drop_index("ix_generated_media_user_id", table_name="generated_media")
    op.drop_index("ix_generated_media_video_id", table_name="generated_media")
    op.drop_index("ix_generated_media_project_id", table_name="generated_media")
    op.drop_index("ix_generated_media_id", table_name="generated_media")
    op.drop_table("generated_media")
