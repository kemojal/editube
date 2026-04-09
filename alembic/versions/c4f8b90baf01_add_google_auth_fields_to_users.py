"""add google auth fields to users

Revision ID: c4f8b90baf01
Revises: 73029b4ebc0d
Create Date: 2026-04-09 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c4f8b90baf01"
down_revision = "73029b4ebc0d"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("auth_provider", sa.String(), nullable=True, server_default="local"))
        batch_op.add_column(sa.Column("google_sub", sa.String(), nullable=True))
        batch_op.alter_column("hashed_password", existing_type=sa.String(), nullable=True)
        batch_op.create_index("ix_users_google_sub", ["google_sub"], unique=True)


def downgrade():
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_index("ix_users_google_sub")
        batch_op.drop_column("google_sub")
        batch_op.drop_column("auth_provider")
        batch_op.alter_column("hashed_password", existing_type=sa.String(), nullable=False)
