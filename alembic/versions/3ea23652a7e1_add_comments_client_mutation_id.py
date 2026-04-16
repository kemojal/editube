"""add comments client mutation id

Revision ID: 3ea23652a7e1
Revises: 5cff8e53a399
Create Date: 2026-04-15 17:12:48.561342

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '3ea23652a7e1'
down_revision = '5cff8e53a399'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("comments", sa.Column("client_mutation_id", sa.String(), nullable=True))
    op.create_index(op.f("ix_comments_client_mutation_id"), "comments", ["client_mutation_id"], unique=False)


def downgrade():
    op.drop_index(op.f("ix_comments_client_mutation_id"), table_name="comments")
    op.drop_column("comments", "client_mutation_id")
