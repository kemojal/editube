"""Widen videos.file_path for long source URLs (e.g. yt-dlp stream URLs).

Revision ID: a0b1c2d3e4f5
Revises: b9c8d7e6f5a4
Create Date: 2026-04-23
"""

from alembic import op
import sqlalchemy as sa


revision = "a0b1c2d3e4f5"
down_revision = "b9c8d7e6f5a4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "videos",
        "file_path",
        existing_type=sa.VARCHAR(length=255),
        type_=sa.Text(),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "videos",
        "file_path",
        existing_type=sa.Text(),
        type_=sa.VARCHAR(length=255),
        existing_nullable=False,
        postgresql_using="left(file_path, 255)",
    )
