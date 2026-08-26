"""review status spine: status provenance, approval records, comment carry-forward

Adds the columns and table the review loop needs to close:

* videos.status_changed_at / status_changed_by — who moved this cut and when
* videos.review_due_at                          — deadline set by "send for review"
* videos.version_notes                          — "what changed in this version"
* video_approvals                               — append-only decision history,
                                                  written by both team and guest paths
* comments.carried_from_comment_id              — links a carried-forward change
                                                  request back to its original

No backfill. `videos.status` already defaults to 'in_progress'; leaving
status_changed_at NULL on existing rows is honest, because we genuinely do not
know when those videos last moved.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-14
"""

from alembic import op
import sqlalchemy as sa

revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("videos", sa.Column("status_changed_at", sa.TIMESTAMP(), nullable=True))
    op.add_column("videos", sa.Column("status_changed_by", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_videos_status_changed_by_users",
        "videos",
        "users",
        ["status_changed_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column("videos", sa.Column("review_due_at", sa.TIMESTAMP(), nullable=True))
    op.add_column("videos", sa.Column("version_notes", sa.Text(), nullable=True))

    op.create_table(
        "video_approvals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "video_id",
            sa.Integer(),
            sa.ForeignKey("videos.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column(
            "actor_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "review_session_id",
            sa.Integer(),
            sa.ForeignKey("review_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "review_link_id",
            sa.Integer(),
            sa.ForeignKey("review_links.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("superseded_at", sa.TIMESTAMP(), nullable=True),
        sa.Column(
            "superseded_by_video_id",
            sa.Integer(),
            sa.ForeignKey("videos.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_video_approvals_video_id", "video_approvals", ["video_id"])
    op.create_index("ix_video_approvals_decision", "video_approvals", ["decision"])
    # The inbox's "recently closed" section reads live decisions per video.
    op.create_index(
        "ix_video_approvals_video_live",
        "video_approvals",
        ["video_id", "superseded_at"],
    )

    op.add_column(
        "comments", sa.Column("carried_from_comment_id", sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        "fk_comments_carried_from_comment_id",
        "comments",
        "comments",
        ["carried_from_comment_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_comments_carried_from_comment_id", "comments", ["carried_from_comment_id"]
    )


def downgrade():
    op.drop_index("ix_comments_carried_from_comment_id", table_name="comments")
    op.drop_constraint("fk_comments_carried_from_comment_id", "comments", type_="foreignkey")
    op.drop_column("comments", "carried_from_comment_id")

    op.drop_index("ix_video_approvals_video_live", table_name="video_approvals")
    op.drop_index("ix_video_approvals_decision", table_name="video_approvals")
    op.drop_index("ix_video_approvals_video_id", table_name="video_approvals")
    op.drop_table("video_approvals")

    op.drop_column("videos", "version_notes")
    op.drop_column("videos", "review_due_at")
    op.drop_constraint("fk_videos_status_changed_by_users", "videos", type_="foreignkey")
    op.drop_column("videos", "status_changed_by")
    op.drop_column("videos", "status_changed_at")
