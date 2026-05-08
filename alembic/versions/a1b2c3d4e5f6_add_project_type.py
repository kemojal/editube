"""add project_type to projects

Revision ID: a1b2c3d4e5f6
Revises: f5g6h7i8j9k0
Create Date: 2026-05-08 10:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "a1b2c3d4e5f6"
down_revision = "f5g6h7i8j9k0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("project_type", sa.String(), nullable=True),
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_projects_project_type ON projects (project_type)")


def downgrade() -> None:
    op.drop_index(op.f("ix_projects_project_type"), table_name="projects")
    op.drop_column("projects", "project_type")
