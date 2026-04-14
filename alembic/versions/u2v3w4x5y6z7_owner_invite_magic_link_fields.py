"""add owner invite fields for review magic tokens

Revision ID: u2v3w4x5y6z7
Revises: t1u2v3w4x5y6
Create Date: 2026-04-14
"""

from alembic import op
import sqlalchemy as sa

revision = "u2v3w4x5y6z7"
down_revision = "t1u2v3w4x5y6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("review_magic_tokens", sa.Column("guest_name", sa.String(), nullable=True))
    op.add_column(
        "review_magic_tokens",
        sa.Column("invited_by_user_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "review_magic_tokens",
        sa.Column(
            "source",
            sa.String(),
            server_default=sa.text("'self_service'"),
            nullable=False,
        ),
    )
    op.create_foreign_key(
        "fk_review_magic_tokens_invited_by_user_id",
        "review_magic_tokens",
        "users",
        ["invited_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_review_magic_tokens_invited_by_user_id",
        "review_magic_tokens",
        type_="foreignkey",
    )
    op.drop_column("review_magic_tokens", "source")
    op.drop_column("review_magic_tokens", "invited_by_user_id")
    op.drop_column("review_magic_tokens", "guest_name")
