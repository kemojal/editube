"""notification grouping: actor, read_at, and coalescing keys

Ten comments used to mean ten rows and ten pushes. `group_key` +
`group_count` let `app/services/notifications.py` fold siblings raised inside
its window into a single row, which is what makes it safe to finally notify
editors about guest comments at all.

`actor_user_id` lets the UI say who did it without re-fetching the comment;
`read_at` records when a notification was read, not just that it was.

Revision ID: b7c8d9e0f1a2
Revises: c3d4e5f6a7b8
Create Date: 2026-08-14
"""

from alembic import op
import sqlalchemy as sa

revision = "b7c8d9e0f1a2"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("notifications", sa.Column("read_at", sa.TIMESTAMP(), nullable=True))
    op.add_column("notifications", sa.Column("actor_user_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_notifications_actor_user_id_users",
        "notifications",
        "users",
        ["actor_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column("notifications", sa.Column("group_key", sa.String(), nullable=True))
    op.add_column(
        "notifications",
        sa.Column("group_count", sa.Integer(), server_default="1", nullable=False),
    )
    op.create_index("ix_notifications_group_key", "notifications", ["group_key"])
    # The coalescing lookup filters on user + type + key + unread.
    op.create_index(
        "ix_notifications_grouping_lookup",
        "notifications",
        ["user_id", "type", "group_key", "read"],
    )


def downgrade():
    op.drop_index("ix_notifications_grouping_lookup", table_name="notifications")
    op.drop_index("ix_notifications_group_key", table_name="notifications")
    op.drop_column("notifications", "group_count")
    op.drop_column("notifications", "group_key")
    op.drop_constraint(
        "fk_notifications_actor_user_id_users", "notifications", type_="foreignkey"
    )
    op.drop_column("notifications", "actor_user_id")
    op.drop_column("notifications", "read_at")
