"""Rename metadata column to meta_info

Revision ID: 73029b4ebc0d
Revises: 63fe804521b9
Create Date: 2024-05-20 10:33:31.214508

The actual rename is performed in 63fe804521b9_initial_migration. This
revision exists only to preserve the migration chain; an earlier version
incorrectly dropped core tables here.
"""
# revision identifiers, used by Alembic.
revision = '73029b4ebc0d'
down_revision = '63fe804521b9'
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
