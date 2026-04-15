"""Project links to workspace shared assets.

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b5
Create Date: 2026-04-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d1e2f3a4b5c6"
down_revision = "c0d1e2f3a4b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_workspace_asset_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "workspace_asset_id",
            sa.Integer(),
            sa.ForeignKey("workspace_assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("folder_id", sa.Integer(), sa.ForeignKey("folders.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_project_workspace_asset_links_project_id",
        "project_workspace_asset_links",
        ["project_id"],
    )
    op.create_index(
        "ix_project_workspace_asset_links_workspace_asset_id",
        "project_workspace_asset_links",
        ["workspace_asset_id"],
    )
    op.create_index(
        "ix_project_workspace_asset_links_folder_id",
        "project_workspace_asset_links",
        ["folder_id"],
    )
    op.create_unique_constraint(
        "uq_project_workspace_asset",
        "project_workspace_asset_links",
        ["project_id", "workspace_asset_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_project_workspace_asset", "project_workspace_asset_links", type_="unique")
    op.drop_index("ix_project_workspace_asset_links_folder_id", table_name="project_workspace_asset_links")
    op.drop_index("ix_project_workspace_asset_links_workspace_asset_id", table_name="project_workspace_asset_links")
    op.drop_index("ix_project_workspace_asset_links_project_id", table_name="project_workspace_asset_links")
    op.drop_table("project_workspace_asset_links")
