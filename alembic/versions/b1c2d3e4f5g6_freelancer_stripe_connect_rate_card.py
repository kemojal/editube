"""freelancer: Stripe Connect on users, invoice connect id, project rate card

Revision ID: b1c2d3e4f5g6
Revises: z2b3c4d5e6f7
Create Date: 2026-04-15
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "b1c2d3e4f5g6"
down_revision = "z2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("stripe_connect_account_id", sa.String(), nullable=True))
    # Non-null account ids must be unique (PostgreSQL allows multiple NULLs).
    op.create_index(
        "ix_users_stripe_connect_account_id",
        "users",
        ["stripe_connect_account_id"],
        unique=True,
    )
    op.add_column(
        "invoices",
        sa.Column("stripe_connect_account_id", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_invoices_stripe_connect_account_id",
        "invoices",
        ["stripe_connect_account_id"],
    )
    op.add_column(
        "projects",
        sa.Column("rate_card_cents", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("projects", "rate_card_cents")
    op.drop_index("ix_invoices_stripe_connect_account_id", table_name="invoices")
    op.drop_column("invoices", "stripe_connect_account_id")
    op.drop_index("ix_users_stripe_connect_account_id", table_name="users")
    op.drop_column("users", "stripe_connect_account_id")
