"""add ai_results table

Revision ID: r8s9t0u1v2w3
Revises: q3r4s5t6u7v8
Create Date: 2026-04-14
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "r8s9t0u1v2w3"
down_revision = "q3r4s5t6u7v8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ai_results",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("video_id", sa.Integer(), nullable=False),
        sa.Column("result_type", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default="completed", nullable=False),
        sa.Column("result_data", JSONB(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ai_results_video_id"), "ai_results", ["video_id"], unique=False)
    op.create_index(
        op.f("ix_ai_results_result_type"),
        "ai_results",
        ["result_type"],
        unique=False,
    )


def downgrade():
    op.drop_index(op.f("ix_ai_results_result_type"), table_name="ai_results")
    op.drop_index(op.f("ix_ai_results_video_id"), table_name="ai_results")
    op.drop_table("ai_results")
