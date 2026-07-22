"""Add aiugc schema and AI UGC ads tables.

Revision ID: aiugc_0001
Revises: c8d9e0f1a2b3
Create Date: 2026-05-29
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "aiugc_0001"
down_revision = "c8d9e0f1a2b3"
branch_labels = None
depends_on = None


def _ts(col: str) -> sa.Column:
    return sa.Column(col, sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False)


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS aiugc")

    op.create_table(
        "ugc_products",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(), server_default="landing", nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("brand", sa.String(), nullable=True),
        sa.Column("price", sa.String(), nullable=True),
        sa.Column("currency", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("benefits", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("pain_points", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("use_cases", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("target_audience", postgresql.JSONB(), nullable=True),
        sa.Column("reviews", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("image_urls", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("raw_scrape", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        schema="aiugc",
    )
    op.create_index("ix_aiugc_products_workspace_id", "ugc_products", ["workspace_id"], schema="aiugc")
    op.create_index("ix_aiugc_products_user_id", "ugc_products", ["user_id"], schema="aiugc")
    op.create_index("ix_aiugc_products_status", "ugc_products", ["status"], schema="aiugc")

    op.create_table(
        "ugc_briefs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("audience", sa.Text(), nullable=True),
        sa.Column("main_promise", sa.Text(), nullable=True),
        sa.Column("pain_points", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("objections", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("benefits", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("angles", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("hooks", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("scripts", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("ctas", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("model_meta", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.ForeignKeyConstraint(["product_id"], ["aiugc.ugc_products.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        schema="aiugc",
    )
    op.create_index("ix_aiugc_briefs_product_id", "ugc_briefs", ["product_id"], schema="aiugc")
    op.create_index("ix_aiugc_briefs_status", "ugc_briefs", ["status"], schema="aiugc")

    op.create_table(
        "ugc_avatars",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(), server_default="stub", nullable=False),
        sa.Column("provider_avatar_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("thumbnail_url", sa.String(), nullable=True),
        sa.Column("age_range", sa.String(), nullable=True),
        sa.Column("gender_presentation", sa.String(), nullable=True),
        sa.Column("region", sa.String(), nullable=True),
        sa.Column("default_voice_id", sa.String(), nullable=True),
        sa.Column("accent", sa.String(), nullable=True),
        sa.Column("energy", sa.String(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("is_premium", sa.Boolean(), server_default="false", nullable=False),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id"),
        schema="aiugc",
    )
    op.create_index("ix_aiugc_avatars_provider", "ugc_avatars", ["provider"], schema="aiugc")

    op.create_table(
        "ugc_voices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(), server_default="stub", nullable=False),
        sa.Column("provider_voice_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("gender", sa.String(), nullable=True),
        sa.Column("accent", sa.String(), nullable=True),
        sa.Column("language", sa.String(), server_default="en", nullable=True),
        sa.Column("preview_url", sa.String(), nullable=True),
        sa.Column("is_premium", sa.Boolean(), server_default="false", nullable=False),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id"),
        schema="aiugc",
    )
    op.create_index("ix_aiugc_voices_provider", "ugc_voices", ["provider"], schema="aiugc")

    op.create_table(
        "ugc_campaigns",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("brief_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(), server_default="Untitled campaign", nullable=False),
        sa.Column("platform", sa.String(), server_default="tiktok", nullable=False),
        sa.Column("default_aspect_ratio", sa.String(), server_default="9:16", nullable=False),
        sa.Column("default_length_sec", sa.Integer(), server_default="30", nullable=False),
        sa.Column("settings", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(), server_default="draft", nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["aiugc.ugc_products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["brief_id"], ["aiugc.ugc_briefs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        schema="aiugc",
    )
    op.create_index("ix_aiugc_campaigns_workspace_id", "ugc_campaigns", ["workspace_id"], schema="aiugc")
    op.create_index("ix_aiugc_campaigns_product_id", "ugc_campaigns", ["product_id"], schema="aiugc")
    op.create_index("ix_aiugc_campaigns_status", "ugc_campaigns", ["status"], schema="aiugc")

    op.create_table(
        "ugc_variations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), server_default="UGC ad", nullable=False),
        sa.Column("angle", sa.String(), nullable=True),
        sa.Column("hook", sa.Text(), nullable=True),
        sa.Column("script", sa.Text(), nullable=True),
        sa.Column("cta", sa.String(), nullable=True),
        sa.Column("caption_style", postgresql.JSONB(), nullable=True),
        sa.Column("provider", sa.String(), server_default="stub", nullable=False),
        sa.Column("provider_avatar_id", sa.String(), nullable=True),
        sa.Column("provider_voice_id", sa.String(), nullable=True),
        sa.Column("avatar_name", sa.String(), nullable=True),
        sa.Column("voice_name", sa.String(), nullable=True),
        sa.Column("aspect_ratio", sa.String(), server_default="9:16", nullable=False),
        sa.Column("length_sec", sa.Integer(), server_default="30", nullable=False),
        sa.Column("music_url", sa.String(), nullable=True),
        sa.Column("brand_logo_url", sa.String(), nullable=True),
        sa.Column("provider_job_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), server_default="draft", nullable=False),
        sa.Column("render_progress", sa.Integer(), server_default="0", nullable=False),
        sa.Column("render_error", sa.Text(), nullable=True),
        sa.Column("storage_url", sa.String(), nullable=True),
        sa.Column("thumbnail_url", sa.String(), nullable=True),
        sa.Column("rq_job_id", sa.String(), nullable=True),
        sa.Column("is_ai_generated", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("disclosure_applied", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("completed_at", sa.TIMESTAMP(), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.ForeignKeyConstraint(["campaign_id"], ["aiugc.ugc_campaigns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        schema="aiugc",
    )
    op.create_index("ix_aiugc_variations_campaign_id", "ugc_variations", ["campaign_id"], schema="aiugc")
    op.create_index("ix_aiugc_variations_status", "ugc_variations", ["status"], schema="aiugc")

    op.create_table(
        "ugc_performance",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("variation_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(), server_default="manual", nullable=False),
        sa.Column("spend", sa.Float(), nullable=True),
        sa.Column("impressions", sa.Integer(), nullable=True),
        sa.Column("clicks", sa.Integer(), nullable=True),
        sa.Column("ctr", sa.Float(), nullable=True),
        sa.Column("conversions", sa.Integer(), nullable=True),
        sa.Column("cvr", sa.Float(), nullable=True),
        sa.Column("roas", sa.Float(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        _ts("captured_at"),
        sa.ForeignKeyConstraint(["variation_id"], ["aiugc.ugc_variations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        schema="aiugc",
    )
    op.create_index("ix_aiugc_performance_variation_id", "ugc_performance", ["variation_id"], schema="aiugc")

    op.create_table(
        "ugc_credit_ledger",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("variation_id", sa.Integer(), nullable=True),
        sa.Column("period", sa.String(), nullable=True),
        sa.Column("balance_after", sa.Integer(), server_default="0", nullable=False),
        _ts("created_at"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["variation_id"], ["aiugc.ugc_variations.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        schema="aiugc",
    )
    op.create_index("ix_aiugc_ledger_workspace_id", "ugc_credit_ledger", ["workspace_id"], schema="aiugc")
    op.create_index("ix_aiugc_ledger_period", "ugc_credit_ledger", ["period"], schema="aiugc")

    # Seed the stub provider catalog so the picker is populated out of the box.
    op.execute(
        """
        INSERT INTO aiugc.ugc_avatars
          (provider, provider_avatar_id, name, age_range, gender_presentation, region, default_voice_id, accent, energy, is_premium)
        VALUES
          ('stub','ugc_f_us_1','Maya (US, energetic)','18-24','female','US','v_f_us_warm','American','high',false),
          ('stub','ugc_f_uk_1','Ella (UK, calm)','25-34','female','UK','v_f_uk_soft','British','medium',false),
          ('stub','ugc_m_us_1','Jordan (US, founder)','25-34','male','US','v_m_us_direct','American','medium',false),
          ('stub','ugc_m_au_1','Leo (AU, friendly)','18-24','male','AU','v_m_au_bright','Australian','high',false),
          ('stub','ugc_f_ca_1','Nova (CA, lifestyle)','25-34','female','CA','v_f_us_warm','Canadian','medium',true)
        """
    )
    op.execute(
        """
        INSERT INTO aiugc.ugc_voices
          (provider, provider_voice_id, name, gender, accent, language)
        VALUES
          ('stub','v_f_us_warm','Warm Female (US)','female','American','en'),
          ('stub','v_f_uk_soft','Soft Female (UK)','female','British','en'),
          ('stub','v_m_us_direct','Direct Male (US)','male','American','en'),
          ('stub','v_m_au_bright','Bright Male (AU)','male','Australian','en')
        """
    )


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS aiugc CASCADE")
