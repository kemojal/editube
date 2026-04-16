"""Add delivery and retention tables.

Revision ID: e1f2a3b4c5d6
Revises: d1e2f3a4b5c6
Create Date: 2026-04-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e1f2a3b4c5d6"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "delivery_exports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("video_id", sa.Integer(), sa.ForeignKey("videos.id", ondelete="CASCADE"), nullable=False),
        sa.Column("profile_key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column("output_path", sa.String(), nullable=True),
        sa.Column("mime_type", sa.String(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_delivery_exports_video_id", "delivery_exports", ["video_id"])
    op.create_index("ix_delivery_exports_profile_key", "delivery_exports", ["profile_key"])

    op.create_table(
        "delivery_packages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("video_id", sa.Integer(), sa.ForeignKey("videos.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column("requested_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("approved_version_id", sa.Integer(), sa.ForeignKey("videos.id", ondelete="SET NULL"), nullable=True),
        sa.Column("zip_url", sa.String(), nullable=True),
        sa.Column("zip_size_bytes", sa.Integer(), nullable=True),
        sa.Column("checksum_sha256", sa.String(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_delivery_packages_project_id", "delivery_packages", ["project_id"])
    op.create_index("ix_delivery_packages_video_id", "delivery_packages", ["video_id"])

    op.create_table(
        "delivery_assets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("delivery_package_id", sa.Integer(), sa.ForeignKey("delivery_packages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("asset_type", sa.String(), nullable=False),
        sa.Column("file_url", sa.String(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("mime_type", sa.String(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("checksum_sha256", sa.String(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_delivery_assets_delivery_package_id", "delivery_assets", ["delivery_package_id"])
    op.create_index("ix_delivery_assets_asset_type", "delivery_assets", ["asset_type"])

    op.create_table(
        "delivery_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("delivery_package_id", sa.Integer(), sa.ForeignKey("delivery_packages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token", sa.String(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("renew_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_revoked", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("last_renewed_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
    )
    op.create_unique_constraint("uq_delivery_links_token", "delivery_links", ["token"])
    op.create_index("ix_delivery_links_token", "delivery_links", ["token"])
    op.create_index("ix_delivery_links_delivery_package_id", "delivery_links", ["delivery_package_id"])
    op.create_index("ix_delivery_links_expires_at", "delivery_links", ["expires_at"])

    op.create_table(
        "delivery_receipts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("delivery_link_id", sa.Integer(), sa.ForeignKey("delivery_links.id", ondelete="CASCADE"), nullable=False),
        sa.Column("delivery_asset_id", sa.Integer(), sa.ForeignKey("delivery_assets.id", ondelete="CASCADE"), nullable=True),
        sa.Column("downloaded_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("guest_name", sa.String(), nullable=True),
        sa.Column("guest_email", sa.String(), nullable=True),
        sa.Column("ip_address", sa.String(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
    )
    op.create_index("ix_delivery_receipts_delivery_link_id", "delivery_receipts", ["delivery_link_id"])
    op.create_index("ix_delivery_receipts_delivery_asset_id", "delivery_receipts", ["delivery_asset_id"])
    op.create_index("ix_delivery_receipts_downloaded_at", "delivery_receipts", ["downloaded_at"])

    op.create_table(
        "project_retention_policies",
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("auto_archive_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("archive_after_days", sa.Integer(), server_default="90", nullable=False),
        sa.Column("cold_tier_provider", sa.String(), nullable=True),
        sa.Column("last_archive_run_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "project_archive_states",
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("state", sa.String(), server_default="active", nullable=False),
        sa.Column("archived_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("cold_moved_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("storage_location_meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("project_archive_states")
    op.drop_table("project_retention_policies")

    op.drop_index("ix_delivery_receipts_downloaded_at", table_name="delivery_receipts")
    op.drop_index("ix_delivery_receipts_delivery_asset_id", table_name="delivery_receipts")
    op.drop_index("ix_delivery_receipts_delivery_link_id", table_name="delivery_receipts")
    op.drop_table("delivery_receipts")

    op.drop_index("ix_delivery_links_expires_at", table_name="delivery_links")
    op.drop_index("ix_delivery_links_delivery_package_id", table_name="delivery_links")
    op.drop_index("ix_delivery_links_token", table_name="delivery_links")
    op.drop_constraint("uq_delivery_links_token", "delivery_links", type_="unique")
    op.drop_table("delivery_links")

    op.drop_index("ix_delivery_assets_asset_type", table_name="delivery_assets")
    op.drop_index("ix_delivery_assets_delivery_package_id", table_name="delivery_assets")
    op.drop_table("delivery_assets")

    op.drop_index("ix_delivery_packages_video_id", table_name="delivery_packages")
    op.drop_index("ix_delivery_packages_project_id", table_name="delivery_packages")
    op.drop_table("delivery_packages")

    op.drop_index("ix_delivery_exports_profile_key", table_name="delivery_exports")
    op.drop_index("ix_delivery_exports_video_id", table_name="delivery_exports")
    op.drop_table("delivery_exports")
