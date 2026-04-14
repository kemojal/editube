"""add workspace_name to settings and user sessions

Revision ID: x9y8z7w6v5u4
Revises: w1x2y3z4a5b6
Create Date: 2026-04-14
"""

from alembic import op
import sqlalchemy as sa

revision = "x9y8z7w6v5u4"
down_revision = "w1x2y3z4a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("workspace_name", sa.String(), nullable=False, server_default="My Workspace"),
    )

    op.create_table(
        "user_sessions",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("last_activity_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("revoked_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])
    op.create_index("ix_user_sessions_session_id", "user_sessions", ["session_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_user_sessions_session_id", table_name="user_sessions")
    op.drop_index("ix_user_sessions_user_id", table_name="user_sessions")
    op.drop_table("user_sessions")
    op.drop_column("user_settings", "workspace_name")
