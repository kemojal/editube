"""add editor integration tables

Revision ID: a2b3c4d5e6f7
Revises: z3a4b5c6d7e8
Create Date: 2026-04-18 02:20:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = "a2b3c4d5e6f7"
down_revision = "z3a4b5c6d7e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── video_proxies ─────────────────────────────────────────────────
    op.create_table(
        "video_proxies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("video_id", sa.Integer(), nullable=False),
        sa.Column("profile", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("bitrate_kbps", sa.Integer(), nullable=True),
        sa.Column("codec", sa.String(), nullable=True),
        sa.Column("file_url", sa.String(), nullable=True),
        sa.Column("file_path_local", sa.String(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("duration", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["video_id"], ["videos.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_video_proxies_id", "video_proxies", ["id"])
    op.create_index("ix_video_proxies_video_id", "video_proxies", ["video_id"])
    op.create_index("ix_video_proxies_profile", "video_proxies", ["profile"])

    # ── watch_folder_configs ──────────────────────────────────────────
    op.create_table(
        "watch_folder_configs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("folder_path", sa.String(), nullable=False),
        sa.Column("auto_proxy", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("auto_version", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("file_pattern", sa.String(), server_default="*", nullable=False),
        sa.Column("last_sync_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_watch_folder_configs_id", "watch_folder_configs", ["id"])
    op.create_index("ix_watch_folder_configs_user_id", "watch_folder_configs", ["user_id"])
    op.create_index("ix_watch_folder_configs_project_id", "watch_folder_configs", ["project_id"])

    # ── nle_sessions ──────────────────────────────────────────────────
    op.create_table(
        "nle_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("nle_type", sa.String(), nullable=False),
        sa.Column("nle_version", sa.String(), nullable=True),
        sa.Column("host_name", sa.String(), nullable=True),
        sa.Column("last_sync_at", sa.TIMESTAMP(), nullable=True),
        sa.Column(
            "sync_direction",
            sa.String(),
            server_default="bidirectional",
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("extra", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_nle_sessions_id", "nle_sessions", ["id"])
    op.create_index("ix_nle_sessions_user_id", "nle_sessions", ["user_id"])
    op.create_index("ix_nle_sessions_project_id", "nle_sessions", ["project_id"])
    op.create_index("ix_nle_sessions_nle_type", "nle_sessions", ["nle_type"])


def downgrade() -> None:
    op.drop_table("nle_sessions")
    op.drop_table("watch_folder_configs")
    op.drop_table("video_proxies")
