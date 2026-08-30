"""Harness auto-apply grants (plan Phase 5, controlled autonomy).

One-shot consent rows the server spends exactly once, plus the
`auto_applied` mark on runs so an automatically applied run is
distinguishable forever from one a human approved.

Revision ID: eh3008300001
Revises: eh2908290001
Create Date: 2026-08-30
"""

from alembic import op
import sqlalchemy as sa

revision = "eh3008300001"
down_revision = "eh2908290001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "editing_harness_auto_apply_grants",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column(
            "project_id",
            sa.Integer(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "video_id",
            sa.Integer(),
            sa.ForeignKey("videos.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("recipe_id", sa.String(), nullable=False),
        sa.Column("spent_at", sa.TIMESTAMP(), nullable=True),
        sa.Column(
            "spent_run_id",
            sa.Integer(),
            sa.ForeignKey("editing_harness_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
    )
    op.add_column(
        "editing_harness_runs",
        sa.Column("auto_applied", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("editing_harness_runs", "auto_applied")
    op.drop_table("editing_harness_auto_apply_grants")
