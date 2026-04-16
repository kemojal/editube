"""add workspace invite notification fields

Revision ID: a7b8c9d0e1f2
Revises: 5cff8e53a399
Create Date: 2026-04-16 10:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a7b8c9d0e1f2"
down_revision = "5cff8e53a399"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("notifications", sa.Column("workspace_id", sa.Integer(), nullable=True))
    op.add_column("notifications", sa.Column("workspace_invite_id", sa.Integer(), nullable=True))
    op.add_column("notifications", sa.Column("invite_token", sa.String(), nullable=True))
    op.add_column("notifications", sa.Column("message", sa.Text(), nullable=True))

    op.create_index(op.f("ix_notifications_workspace_id"), "notifications", ["workspace_id"], unique=False)
    op.create_index(
        op.f("ix_notifications_workspace_invite_id"),
        "notifications",
        ["workspace_invite_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_notifications_workspace_id",
        "notifications",
        "workspaces",
        ["workspace_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_notifications_workspace_invite_id",
        "notifications",
        "workspace_invites",
        ["workspace_invite_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_notifications_workspace_invite_id", "notifications", type_="foreignkey")
    op.drop_constraint("fk_notifications_workspace_id", "notifications", type_="foreignkey")
    op.drop_index(op.f("ix_notifications_workspace_invite_id"), table_name="notifications")
    op.drop_index(op.f("ix_notifications_workspace_id"), table_name="notifications")
    op.drop_column("notifications", "message")
    op.drop_column("notifications", "invite_token")
    op.drop_column("notifications", "workspace_invite_id")
    op.drop_column("notifications", "workspace_id")
