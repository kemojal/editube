"""Add guest_avatar_url to comments for review avatars."""

from alembic import op
import sqlalchemy as sa

revision = "c0d1e2f3a4b5"
down_revision = "n8o9p0q1r2s3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "comments",
        sa.Column("guest_avatar_url", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("comments", "guest_avatar_url")
