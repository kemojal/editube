"""add users.workflow_types (multi-select onboarding workflows)

Onboarding used to ask for one workflow and store it in the singular
`workflow_type` column. The three workflows are not exclusive, so the answer is
now a list. `workflow_type` is left in place — it still holds the retired
org-type answers ("agency" / "freelancer" / "internal") from before that step
asked about the work — but nothing writes it any more.

Revision ID: f1a2b3c4d5e6
Revises: a9b1c2d3e4f5
Create Date: 2026-08-05
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "f1a2b3c4d5e6"
down_revision = "a9b1c2d3e4f5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("workflow_types", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    # Carry over the single-workflow answers as one-element lists. Org-type
    # values are deliberately not migrated: they are not an answer to the
    # question this step asks now, so those users are asked again.
    op.execute(
        """
        UPDATE users
           SET workflow_types = jsonb_build_array(workflow_type)
         WHERE workflow_type IN ('auto_edit', 'repurpose', 'review')
        """
    )


def downgrade() -> None:
    op.drop_column("users", "workflow_types")
