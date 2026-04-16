"""collab qol foundations

Revision ID: l4m5n6o7p8q9
Revises: d1e2f3a4b5c6
Create Date: 2026-04-15 14:30:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "l4m5n6o7p8q9"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("comments", sa.Column("transcript_segment_index", sa.Integer(), nullable=True))
    op.add_column("comments", sa.Column("word_start_index", sa.Integer(), nullable=True))
    op.add_column("comments", sa.Column("word_end_index", sa.Integer(), nullable=True))
    op.add_column("comments", sa.Column("anchor_text", sa.Text(), nullable=True))

    op.create_table(
        "review_room_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("review_link_id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["review_link_id"], ["review_links.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["review_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_review_room_messages_id"), "review_room_messages", ["id"], unique=False)
    op.create_index(
        op.f("ix_review_room_messages_review_link_id"),
        "review_room_messages",
        ["review_link_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_review_room_messages_session_id"),
        "review_room_messages",
        ["session_id"],
        unique=False,
    )

    op.create_table(
        "review_recording_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("review_link_id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), server_default="processing", nullable=False),
        sa.Column("file_url", sa.Text(), nullable=True),
        sa.Column("storage_key", sa.Text(), nullable=True),
        sa.Column("mime_type", sa.String(), nullable=True),
        sa.Column("bytes_size", sa.Integer(), nullable=True),
        sa.Column("consent_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("ended_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("archived_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("deleted_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("retention_days", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["review_link_id"], ["review_links.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["review_sessions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_review_recording_sessions_id"),
        "review_recording_sessions",
        ["id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_review_recording_sessions_review_link_id"),
        "review_recording_sessions",
        ["review_link_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_review_recording_sessions_session_id"),
        "review_recording_sessions",
        ["session_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_review_recording_sessions_session_id"), table_name="review_recording_sessions")
    op.drop_index(op.f("ix_review_recording_sessions_review_link_id"), table_name="review_recording_sessions")
    op.drop_index(op.f("ix_review_recording_sessions_id"), table_name="review_recording_sessions")
    op.drop_table("review_recording_sessions")

    op.drop_index(op.f("ix_review_room_messages_session_id"), table_name="review_room_messages")
    op.drop_index(op.f("ix_review_room_messages_review_link_id"), table_name="review_room_messages")
    op.drop_index(op.f("ix_review_room_messages_id"), table_name="review_room_messages")
    op.drop_table("review_room_messages")

    op.drop_column("comments", "anchor_text")
    op.drop_column("comments", "word_end_index")
    op.drop_column("comments", "word_start_index")
    op.drop_column("comments", "transcript_segment_index")
