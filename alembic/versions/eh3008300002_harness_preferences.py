"""Harness preference-learning settings (plan Phase 5).

One JSONB column on user_settings: the enabled flag and the reset cutoff.
Learned values are never stored — they are recomputed from run history.

Revision ID: eh3008300002
Revises: eh3008300001
Create Date: 2026-08-30
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "eh3008300002"
down_revision = "eh3008300001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("harness_preferences", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "harness_preferences")
