"""add review_links, review_sessions, review_events and guest comment fields

Revision ID: q3r4s5t6u7v8
Revises: p2q3r4s5t6u7
Create Date: 2026-04-14

"""

from alembic import op
import sqlalchemy as sa


revision = "q3r4s5t6u7v8"
down_revision = "p2q3r4s5t6u7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "review_links",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column(
            "video_id",
            sa.Integer(),
            sa.ForeignKey("videos.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "created_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("token", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column("password_hash", sa.String(), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(), nullable=True),
        sa.Column(
            "allow_download",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "allow_comments",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "watermark_enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "require_email",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.TIMESTAMP(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_review_links_token", "review_links", ["token"], unique=True
    )

    op.create_table(
        "review_sessions",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column(
            "review_link_id",
            sa.Integer(),
            sa.ForeignKey("review_links.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("fingerprint", sa.String(), nullable=False, index=True),
        sa.Column("guest_name", sa.String(), nullable=True),
        sa.Column("guest_email", sa.String(), nullable=True),
        sa.Column("ip_address", sa.String(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column(
            "total_watch_seconds",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "max_position",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "reached_end",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "view_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "first_viewed_at",
            sa.TIMESTAMP(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_viewed_at",
            sa.TIMESTAMP(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("approved_at", sa.TIMESTAMP(), nullable=True),
    )

    op.create_table(
        "review_events",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column(
            "session_id",
            sa.Integer(),
            sa.ForeignKey("review_sessions.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("range_end", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # Guest-comment columns on existing comments table
    op.alter_column("comments", "user_id", existing_type=sa.Integer(), nullable=True)
    op.add_column(
        "comments", sa.Column("guest_name", sa.String(), nullable=True)
    )
    op.add_column(
        "comments", sa.Column("guest_email", sa.String(), nullable=True)
    )
    op.add_column(
        "comments",
        sa.Column(
            "review_link_id",
            sa.Integer(),
            sa.ForeignKey("review_links.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("comments", "review_link_id")
    op.drop_column("comments", "guest_email")
    op.drop_column("comments", "guest_name")
    op.alter_column("comments", "user_id", existing_type=sa.Integer(), nullable=False)
    op.drop_table("review_events")
    op.drop_table("review_sessions")
    op.drop_index("ix_review_links_token", table_name="review_links")
    op.drop_table("review_links")
