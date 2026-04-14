"""add onboarding fields to users

Revision ID: e6b2c3d4f5a6
Revises: d5a1b2c3e4f5
Create Date: 2026-04-10 05:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "e6b2c3d4f5a6"
down_revision = "d5a1b2c3e4f5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("full_name", sa.String(), nullable=True))
    op.add_column("users", sa.Column("avatar_url", sa.String(), nullable=True))
    op.add_column("users", sa.Column("phone", sa.String(), nullable=True))
    op.add_column("users", sa.Column("workflow_type", sa.String(), nullable=True))
    op.add_column("users", sa.Column("plan", sa.String(), nullable=True))
    op.add_column("users", sa.Column("onboarding_completed", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("users", sa.Column("trial_start_date", sa.TIMESTAMP(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "trial_start_date")
    op.drop_column("users", "onboarding_completed")
    op.drop_column("users", "plan")
    op.drop_column("users", "workflow_type")
    op.drop_column("users", "phone")
    op.drop_column("users", "avatar_url")
    op.drop_column("users", "full_name")
