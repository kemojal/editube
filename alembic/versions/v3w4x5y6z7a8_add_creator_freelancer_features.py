"""add creator-native + freelancer business layer tables

Revision ID: v3w4x5y6z7a8
Revises: u2v3w4x5y6z7
Create Date: 2026-04-14
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "v3w4x5y6z7a8"
down_revision = "u2v3w4x5y6z7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- Project column additions (freelancer scope) ---
    op.add_column("projects", sa.Column("scope_revisions_included", sa.Integer(), server_default="3", nullable=False))
    op.add_column("projects", sa.Column("revision_count", sa.Integer(), server_default="0", nullable=False))
    op.add_column("projects", sa.Column("change_request_fee_cents", sa.Integer(), server_default="0", nullable=False))
    op.add_column("projects", sa.Column("currency", sa.String(), server_default="USD", nullable=False))
    op.add_column("projects", sa.Column("hourly_rate_cents", sa.Integer(), nullable=True))
    op.add_column("projects", sa.Column("deliverables_locked", sa.Boolean(), server_default=sa.text("true"), nullable=False))
    op.add_column("projects", sa.Column("portfolio_public", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.add_column("projects", sa.Column("portfolio_slug", sa.String(), nullable=True))
    op.add_column("projects", sa.Column("client_name", sa.String(), nullable=True))
    op.add_column("projects", sa.Column("client_email", sa.String(), nullable=True))
    op.create_index("ix_projects_portfolio_slug", "projects", ["portfolio_slug"], unique=True)

    # --- Creator-native tables ---
    op.create_table(
        "video_publications",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("video_id", sa.Integer(), sa.ForeignKey("videos.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("platform", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default="draft", nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("tags", sa.Text(), nullable=True),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("privacy", sa.String(), server_default="private", nullable=False),
        sa.Column("scheduled_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("published_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("external_id", sa.String(), nullable=True),
        sa.Column("external_url", sa.String(), nullable=True),
        sa.Column("thumbnail_variant_id", sa.Integer(), nullable=True),
        sa.Column("extra", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_video_publications_video_id", "video_publications", ["video_id"])

    op.create_table(
        "video_aspect_exports",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("video_id", sa.Integer(), sa.ForeignKey("videos.id", ondelete="CASCADE"), nullable=False),
        sa.Column("aspect_ratio", sa.String(), nullable=False),
        sa.Column("platform_preset", sa.String(), nullable=True),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column("output_path", sa.String(), nullable=True),
        sa.Column("thumbnail_url", sa.String(), nullable=True),
        sa.Column("duration", sa.Integer(), nullable=True),
        sa.Column("subject_tracking", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_video_aspect_exports_video_id", "video_aspect_exports", ["video_id"])

    op.create_table(
        "video_chapters",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("video_id", sa.Integer(), sa.ForeignKey("videos.id", ondelete="CASCADE"), nullable=False),
        sa.Column("start_time", sa.Integer(), nullable=False),
        sa.Column("end_time", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("source", sa.String(), server_default="manual", nullable=False),
        sa.Column("order_index", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_video_chapters_video_id", "video_chapters", ["video_id"])

    op.create_table(
        "video_end_screens",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("video_id", sa.Integer(), sa.ForeignKey("videos.id", ondelete="CASCADE"), unique=True, nullable=False),
        sa.Column("cards", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("pinned_comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "brand_deals",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("video_id", sa.Integer(), sa.ForeignKey("videos.id", ondelete="SET NULL"), nullable=True),
        sa.Column("sponsor_name", sa.String(), nullable=False),
        sa.Column("contact_email", sa.String(), nullable=True),
        sa.Column("amount_cents", sa.Integer(), server_default="0", nullable=False),
        sa.Column("currency", sa.String(), server_default="USD", nullable=False),
        sa.Column("segment_start", sa.Integer(), nullable=True),
        sa.Column("segment_end", sa.Integer(), nullable=True),
        sa.Column("integration_notes", sa.Text(), nullable=True),
        sa.Column("payout_status", sa.String(), server_default="pending", nullable=False),
        sa.Column("paid_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "thumbnail_variants",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("video_id", sa.Integer(), sa.ForeignKey("videos.id", ondelete="CASCADE"), nullable=False),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column("image_url", sa.String(), nullable=False),
        sa.Column("is_winner", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("impressions", sa.Integer(), server_default="0", nullable=False),
        sa.Column("clicks", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_thumbnail_variants_video_id", "thumbnail_variants", ["video_id"])

    # --- Freelancer tables ---
    op.create_table(
        "project_revisions",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("video_id", sa.Integer(), sa.ForeignKey("videos.id", ondelete="SET NULL"), nullable=True),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("triggered_by", sa.String(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("billable", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_project_revisions_project_id", "project_revisions", ["project_id"])

    op.create_table(
        "invoices",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("number", sa.String(), nullable=True),
        sa.Column("client_name", sa.String(), nullable=True),
        sa.Column("client_email", sa.String(), nullable=True),
        sa.Column("currency", sa.String(), server_default="USD", nullable=False),
        sa.Column("subtotal_cents", sa.Integer(), server_default="0", nullable=False),
        sa.Column("tax_cents", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_cents", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(), server_default="draft", nullable=False),
        sa.Column("stripe_invoice_id", sa.String(), nullable=True),
        sa.Column("stripe_payment_link", sa.String(), nullable=True),
        sa.Column("due_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("sent_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("paid_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "invoice_items",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("invoice_id", sa.Integer(), sa.ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Integer(), server_default="1", nullable=False),
        sa.Column("unit_price_cents", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_cents", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_index("ix_invoice_items_invoice_id", "invoice_items", ["invoice_id"])

    op.create_table(
        "project_milestones",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), server_default="0", nullable=False),
        sa.Column("currency", sa.String(), server_default="USD", nullable=False),
        sa.Column("percentage", sa.Integer(), nullable=True),
        sa.Column("due_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column("invoice_id", sa.Integer(), sa.ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True),
        sa.Column("order_index", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_project_milestones_project_id", "project_milestones", ["project_id"])

    op.create_table(
        "contracts",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), server_default="draft", nullable=False),
        sa.Column("signer_name", sa.String(), nullable=True),
        sa.Column("signer_email", sa.String(), nullable=True),
        sa.Column("signature_data", sa.Text(), nullable=True),
        sa.Column("signed_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("signing_token", sa.String(), nullable=True),
        sa.Column("pdf_url", sa.String(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_contracts_project_id", "contracts", ["project_id"])
    op.create_index("ix_contracts_signing_token", "contracts", ["signing_token"], unique=True)

    op.create_table(
        "time_entries",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("ended_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), server_default="0", nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("billable", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("hourly_rate_cents", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_time_entries_project_id", "time_entries", ["project_id"])

    op.create_table(
        "project_estimates",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("runtime_minutes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("complexity", sa.String(), server_default="standard", nullable=False),
        sa.Column("rate_cents_per_hour", sa.Integer(), server_default="0", nullable=False),
        sa.Column("estimated_hours", sa.Integer(), server_default="0", nullable=False),
        sa.Column("line_items", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("total_cents", sa.Integer(), server_default="0", nullable=False),
        sa.Column("currency", sa.String(), server_default="USD", nullable=False),
        sa.Column("status", sa.String(), server_default="draft", nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_project_estimates_project_id", "project_estimates", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_project_estimates_project_id", table_name="project_estimates")
    op.drop_table("project_estimates")
    op.drop_index("ix_time_entries_project_id", table_name="time_entries")
    op.drop_table("time_entries")
    op.drop_index("ix_contracts_signing_token", table_name="contracts")
    op.drop_index("ix_contracts_project_id", table_name="contracts")
    op.drop_table("contracts")
    op.drop_index("ix_project_milestones_project_id", table_name="project_milestones")
    op.drop_table("project_milestones")
    op.drop_index("ix_invoice_items_invoice_id", table_name="invoice_items")
    op.drop_table("invoice_items")
    op.drop_table("invoices")
    op.drop_index("ix_project_revisions_project_id", table_name="project_revisions")
    op.drop_table("project_revisions")

    op.drop_index("ix_thumbnail_variants_video_id", table_name="thumbnail_variants")
    op.drop_table("thumbnail_variants")
    op.drop_table("brand_deals")
    op.drop_table("video_end_screens")
    op.drop_index("ix_video_chapters_video_id", table_name="video_chapters")
    op.drop_table("video_chapters")
    op.drop_index("ix_video_aspect_exports_video_id", table_name="video_aspect_exports")
    op.drop_table("video_aspect_exports")
    op.drop_index("ix_video_publications_video_id", table_name="video_publications")
    op.drop_table("video_publications")

    op.drop_index("ix_projects_portfolio_slug", table_name="projects")
    op.drop_column("projects", "client_email")
    op.drop_column("projects", "client_name")
    op.drop_column("projects", "portfolio_slug")
    op.drop_column("projects", "portfolio_public")
    op.drop_column("projects", "deliverables_locked")
    op.drop_column("projects", "hourly_rate_cents")
    op.drop_column("projects", "currency")
    op.drop_column("projects", "change_request_fee_cents")
    op.drop_column("projects", "revision_count")
    op.drop_column("projects", "scope_revisions_included")
