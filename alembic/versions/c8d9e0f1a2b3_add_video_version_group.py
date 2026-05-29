"""add video version_group_id (version chains)

Revision ID: c8d9e0f1a2b3
Revises: b6c7d8e9f0a1
Create Date: 2026-05-29 15:00:00.000000

Adds a per-deliverable version chain to videos. Each upload that is "a new
version of" an existing video shares its version_group_id; `version` becomes the
per-group ordinal.

Backfill: existing videos each get their OWN group (one video per chain). We
cannot recover historical groupings (none were stored), so pre-existing
multi-upload review projects will show as separate single-version deliverables.
Going forward, "upload new version" links them.
"""

from alembic import op
import sqlalchemy as sa


revision = "c8d9e0f1a2b3"
down_revision = "b6c7d8e9f0a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("videos", sa.Column("version_group_id", sa.String(), nullable=True))
    # Backfill: each existing video becomes its own single-version chain.
    op.execute(
        "UPDATE videos "
        "SET version_group_id = md5(random()::text || clock_timestamp()::text || id::text) "
        "WHERE version_group_id IS NULL"
    )
    op.create_index(
        op.f("ix_videos_version_group_id"),
        "videos",
        ["version_group_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_videos_version_group_id"), table_name="videos")
    op.drop_column("videos", "version_group_id")
