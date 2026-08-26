"""add media metadata to workspace assets

The shared library stored only `title`, `category` and `file_url`, which is
enough for a dropdown and nothing else: an asset browser cannot render a grid
without knowing what the file *is* (mime), how big it is, how long it runs, or
what it looks like. `storage_key` is the object key when the file lives in
R2/Cloudinary rather than on local disk.

`size_bytes` also feeds the plan storage meter — until now workspace assets
were invisible to the cap, which counted `videos.size_bytes` alone.

Revision ID: c7d8e9f0a1b2
Revises: e487ef1b9a01
Create Date: 2026-08-22 02:10:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c7d8e9f0a1b2"
down_revision = "e487ef1b9a01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workspace_assets", sa.Column("mime_type", sa.String(), nullable=True))
    op.add_column(
        "workspace_assets",
        sa.Column("size_bytes", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.add_column("workspace_assets", sa.Column("duration_ms", sa.Integer(), nullable=True))
    op.add_column("workspace_assets", sa.Column("width", sa.Integer(), nullable=True))
    op.add_column("workspace_assets", sa.Column("height", sa.Integer(), nullable=True))
    op.add_column("workspace_assets", sa.Column("thumbnail_url", sa.String(), nullable=True))
    op.add_column("workspace_assets", sa.Column("storage_key", sa.String(), nullable=True))
    # The library lists newest-first inside one workspace; without this the
    # page-one query is a full scan of every workspace's assets.
    op.create_index(
        "ix_workspace_assets_workspace_created",
        "workspace_assets",
        ["workspace_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_workspace_assets_workspace_created", table_name="workspace_assets")
    op.drop_column("workspace_assets", "storage_key")
    op.drop_column("workspace_assets", "thumbnail_url")
    op.drop_column("workspace_assets", "height")
    op.drop_column("workspace_assets", "width")
    op.drop_column("workspace_assets", "duration_ms")
    op.drop_column("workspace_assets", "size_bytes")
    op.drop_column("workspace_assets", "mime_type")
