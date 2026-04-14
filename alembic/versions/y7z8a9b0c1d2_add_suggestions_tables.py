"""add suggestions tables

Revision ID: y7z8a9b0c1d2
Revises: v3w4x5y6z7a8
Create Date: 2026-04-14
"""

from alembic import op
import sqlalchemy as sa

revision = "y7z8a9b0c1d2"
down_revision = "x9y8z7w6v5u4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "suggestions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("status", sa.String(), server_default="open", nullable=False),
        sa.Column("upvotes_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_suggestions_user_id", "suggestions", ["user_id"], unique=False)
    op.create_index("ix_suggestions_status", "suggestions", ["status"], unique=False)
    op.create_index("ix_suggestions_created_at", "suggestions", ["created_at"], unique=False)
    op.create_index("ix_suggestions_upvotes_count", "suggestions", ["upvotes_count"], unique=False)

    op.create_table(
        "suggestion_comments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("suggestion_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["suggestion_id"], ["suggestions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_suggestion_comments_suggestion_id",
        "suggestion_comments",
        ["suggestion_id"],
        unique=False,
    )
    op.create_index("ix_suggestion_comments_user_id", "suggestion_comments", ["user_id"], unique=False)

    op.create_table(
        "suggestion_votes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("suggestion_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["suggestion_id"], ["suggestions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "suggestion_id", "user_id", name="uq_suggestion_votes_suggestion_user"
        ),
    )
    op.create_index("ix_suggestion_votes_suggestion_id", "suggestion_votes", ["suggestion_id"], unique=False)
    op.create_index("ix_suggestion_votes_user_id", "suggestion_votes", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_suggestion_votes_user_id", table_name="suggestion_votes")
    op.drop_index("ix_suggestion_votes_suggestion_id", table_name="suggestion_votes")
    op.drop_table("suggestion_votes")

    op.drop_index("ix_suggestion_comments_user_id", table_name="suggestion_comments")
    op.drop_index("ix_suggestion_comments_suggestion_id", table_name="suggestion_comments")
    op.drop_table("suggestion_comments")

    op.drop_index("ix_suggestions_upvotes_count", table_name="suggestions")
    op.drop_index("ix_suggestions_created_at", table_name="suggestions")
    op.drop_index("ix_suggestions_status", table_name="suggestions")
    op.drop_index("ix_suggestions_user_id", table_name="suggestions")
    op.drop_table("suggestions")
