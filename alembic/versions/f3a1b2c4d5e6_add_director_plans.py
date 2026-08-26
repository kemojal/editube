"""Add director_plans, and link generated media back to the run that asked for it.

Revision ID: f3a1b2c4d5e6
Revises: d2e3f4a5b6c7
Create Date: 2026-08-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f3a1b2c4d5e6"
down_revision = "d2e3f4a5b6c7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "director_plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("video_id", sa.Integer(), sa.ForeignKey("videos.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(), server_default="queued", nullable=False),
        sa.Column("stage", sa.String(), nullable=True),
        sa.Column("progress", sa.Integer(), server_default="0", nullable=False),
        sa.Column("tier", sa.String(), server_default="standard", nullable=False),
        sa.Column("brief", sa.Text(), nullable=True),
        sa.Column("allow_video", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("usage", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("applied_manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("applied_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_director_plans_video_id", "director_plans", ["video_id"])
    op.create_index("ix_director_plans_project_id", "director_plans", ["project_id"])
    op.create_index("ix_director_plans_user_id", "director_plans", ["user_id"])
    op.create_index("ix_director_plans_status", "director_plans", ["status"])

    # Provenance on generated media. `SET NULL` rather than cascade: deleting a
    # plan must not delete the images it produced — they have been paid for, and
    # by then they may be sitting on a timeline the user has since edited.
    op.add_column(
        "generated_media",
        sa.Column("director_plan_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "generated_media",
        sa.Column("director_directive_id", sa.String(), nullable=True),
    )
    op.create_foreign_key(
        "fk_generated_media_director_plan",
        "generated_media",
        "director_plans",
        ["director_plan_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_generated_media_director_plan_id", "generated_media", ["director_plan_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_generated_media_director_plan_id", table_name="generated_media")
    op.drop_constraint(
        "fk_generated_media_director_plan", "generated_media", type_="foreignkey"
    )
    op.drop_column("generated_media", "director_directive_id")
    op.drop_column("generated_media", "director_plan_id")

    op.drop_index("ix_director_plans_status", table_name="director_plans")
    op.drop_index("ix_director_plans_user_id", table_name="director_plans")
    op.drop_index("ix_director_plans_project_id", table_name="director_plans")
    op.drop_index("ix_director_plans_video_id", table_name="director_plans")
    op.drop_table("director_plans")
