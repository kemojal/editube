"""activity_feed rows die with their project (and user) instead of blocking the delete

Revision ID: b2c3d4e5f6a7
Revises: a1d2e3f4a5b6
Create Date: 2026-08-12
"""

from alembic import op

revision = "b2c3d4e5f6a7"
down_revision = "a1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("activity_feed_project_id_fkey", "activity_feed", type_="foreignkey")
    op.create_foreign_key(
        "activity_feed_project_id_fkey",
        "activity_feed",
        "projects",
        ["project_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint("activity_feed_user_id_fkey", "activity_feed", type_="foreignkey")
    op.create_foreign_key(
        "activity_feed_user_id_fkey",
        "activity_feed",
        "users",
        ["user_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade():
    op.drop_constraint("activity_feed_project_id_fkey", "activity_feed", type_="foreignkey")
    op.create_foreign_key(
        "activity_feed_project_id_fkey", "activity_feed", "projects", ["project_id"], ["id"]
    )
    op.drop_constraint("activity_feed_user_id_fkey", "activity_feed", type_="foreignkey")
    op.create_foreign_key(
        "activity_feed_user_id_fkey", "activity_feed", "users", ["user_id"], ["id"]
    )
