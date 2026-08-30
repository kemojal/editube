"""Workspace recipe templates (plan Phase 4/5, team house style).

Revision ID: eh3008300003
Revises: eh3008300002
Create Date: 2026-08-30
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "eh3008300003"
down_revision = "eh3008300002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "editing_harness_recipe_templates",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column(
            "workspace_id",
            sa.Integer(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("recipe_id", sa.String(), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column(
            "updated_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("workspace_id", "recipe_id", name="uq_harness_recipe_template"),
    )


def downgrade() -> None:
    op.drop_table("editing_harness_recipe_templates")
