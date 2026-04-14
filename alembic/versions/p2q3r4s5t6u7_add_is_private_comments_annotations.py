"""add is_private to comments and annotations

Revision ID: p2q3r4s5t6u7
Revises: g8h9i0j1k2l3
Create Date: 2026-04-14

"""

from alembic import op
import sqlalchemy as sa

revision = "p2q3r4s5t6u7"
down_revision = "g8h9i0j1k2l3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "comments",
        sa.Column(
            "is_private",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "annotations",
        sa.Column(
            "is_private",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("annotations", "is_private")
    op.drop_column("comments", "is_private")
