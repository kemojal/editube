"""team video room chat messages

Revision ID: z3a4b5c6d7e8
Revises: q1w2e3r4t5y6
Create Date: 2026-04-17 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "z3a4b5c6d7e8"
down_revision = "q1w2e3r4t5y6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "team_video_room_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("video_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_team_video_room_messages_id"), "team_video_room_messages", ["id"], unique=False)
    op.create_index(
        op.f("ix_team_video_room_messages_video_id"),
        "team_video_room_messages",
        ["video_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_team_video_room_messages_user_id"),
        "team_video_room_messages",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_team_video_room_messages_user_id"), table_name="team_video_room_messages")
    op.drop_index(op.f("ix_team_video_room_messages_video_id"), table_name="team_video_room_messages")
    op.drop_index(op.f("ix_team_video_room_messages_id"), table_name="team_video_room_messages")
    op.drop_table("team_video_room_messages")
