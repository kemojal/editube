"""add google auth fields to users

Revision ID: c4f8b90baf01
Revises: 73029b4ebc0d
Create Date: 2026-04-09 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = "c4f8b90baf01"
down_revision = "73029b4ebc0d"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = inspect(bind)
    cols = {c["name"] for c in insp.get_columns("users")}
    idx_names = {i["name"] for i in insp.get_indexes("users")}

    with op.batch_alter_table("users") as batch_op:
        if "auth_provider" not in cols:
            batch_op.add_column(
                sa.Column("auth_provider", sa.String(), nullable=True, server_default="local")
            )
        if "google_sub" not in cols:
            batch_op.add_column(sa.Column("google_sub", sa.String(), nullable=True))
        batch_op.alter_column("hashed_password", existing_type=sa.String(), nullable=True)

    if "ix_users_google_sub" not in idx_names:
        op.create_index("ix_users_google_sub", "users", ["google_sub"], unique=True)


def downgrade():
    bind = op.get_bind()
    insp = inspect(bind)
    cols = {c["name"] for c in insp.get_columns("users")}
    idx_names = {i["name"] for i in insp.get_indexes("users")}

    if "ix_users_google_sub" in idx_names:
        op.drop_index("ix_users_google_sub", table_name="users")
    with op.batch_alter_table("users") as batch_op:
        if "google_sub" in cols:
            batch_op.drop_column("google_sub")
        if "auth_provider" in cols:
            batch_op.drop_column("auth_provider")
        batch_op.alter_column("hashed_password", existing_type=sa.String(), nullable=False)
