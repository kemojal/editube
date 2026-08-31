"""Add missing indexes on hot-path foreign keys.

The base tables predate Alembic, and Postgres does not index FK columns
automatically. Every dashboard/editor list endpoint filters or joins on the
columns below; without indexes each lookup is a sequential scan that repeats
per project (project list), per comment (comment tree), or per poll tick.

Composite choices:
- videos (project_id, updated_at, id): serves both plain project_id filters
  (prefix) and the latest-source-video ranking used by the project list.
- comments (video_id, created_at): list order for a video's comment thread.
- notifications (user_id, created_at): the notification feed's exact shape.
- ai_results (video_id, result_type): the draft/result lookup done on every
  editor load and pipeline poll (video_id alone is already indexed).

Revision ID: fi3008300004
Revises: eh3008300003
Create Date: 2026-08-30
"""

from alembic import op

revision = "fi3008300004"
down_revision = "eh3008300003"
branch_labels = None
depends_on = None

_INDEXES = [
    ("ix_projects_creator_id", "projects", ["creator_id"], None),
    ("ix_project_collaborators_project_id", "project_collaborators", ["project_id"], None),
    ("ix_project_collaborators_user_id", "project_collaborators", ["user_id"], None),
    ("ix_videos_project_updated", "videos", ["project_id", "updated_at", "id"], None),
    ("ix_videos_folder_id", "videos", ["folder_id"], None),
    ("ix_folders_project_id", "folders", ["project_id"], None),
    ("ix_folders_parent_id", "folders", ["parent_id"], None),
    ("ix_comments_video_created", "comments", ["video_id", "created_at"], None),
    ("ix_comments_parent_id", "comments", ["parent_id"], None),
    ("ix_annotations_video_id", "annotations", ["video_id"], None),
    ("ix_annotations_user_id", "annotations", ["user_id"], None),
    ("ix_notifications_user_created", "notifications", ["user_id", "created_at"], None),
    ("ix_ai_results_video_type", "ai_results", ["video_id", "result_type"], None),
    ("ix_repurpose_jobs_project_id", "repurpose_jobs", ["project_id"], "repurpose"),
]


def upgrade() -> None:
    for name, table, columns, schema in _INDEXES:
        op.create_index(
            name, table, columns, unique=False, schema=schema, if_not_exists=True
        )


def downgrade() -> None:
    for name, table, _columns, schema in reversed(_INDEXES):
        op.drop_index(name, table_name=table, schema=schema, if_exists=True)
