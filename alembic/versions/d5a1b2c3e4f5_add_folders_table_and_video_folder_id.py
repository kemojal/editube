"""add folders table and video folder_id

Revision ID: d5a1b2c3e4f5
Revises: c4f8b90baf01
Create Date: 2026-04-10 04:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "d5a1b2c3e4f5"
down_revision = "c4f8b90baf01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "folders",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("folders.id"), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.func.now()),
    )
    op.create_index("ix_folders_id", "folders", ["id"])
    op.create_index("ix_folders_project_parent", "folders", ["project_id", "parent_id"])

    op.add_column("videos", sa.Column("folder_id", sa.Integer(), sa.ForeignKey("folders.id"), nullable=True))
    op.add_column("videos", sa.Column("thumbnail_url", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("videos", "thumbnail_url")
    op.drop_column("videos", "folder_id")
    op.drop_index("ix_folders_project_parent", table_name="folders")
    op.drop_index("ix_folders_id", table_name="folders")
    op.drop_table("folders")
