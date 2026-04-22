"""Add repurpose clip tables (clips, clip_styles, clip_templates)

Revision ID: q1r2s3t4u5v6
Revises: cdb173bf8fa7
Create Date: 2026-04-22

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "q1r2s3t4u5v6"
down_revision = "cdb173bf8fa7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "clips",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("video_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(), server_default="Untitled clip", nullable=False),
        sa.Column("start_time", sa.Float(), nullable=False),
        sa.Column("end_time", sa.Float(), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("aspect_ratio", sa.String(), server_default="9:16", nullable=False),
        sa.Column("virality_score", sa.Float(), nullable=True),
        sa.Column("status", sa.String(), server_default="draft", nullable=False),
        sa.Column("render_progress", sa.Integer(), server_default="0", nullable=False),
        sa.Column("render_error", sa.Text(), nullable=True),
        sa.Column("storage_path", sa.String(), nullable=True),
        sa.Column("thumbnail_url", sa.String(), nullable=True),
        sa.Column("transcript_text", sa.Text(), nullable=True),
        sa.Column("is_ai_suggested", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("suggestion_reason", sa.Text(), nullable=True),
        sa.Column("hooks_matched", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("preset", sa.String(), nullable=True),
        sa.Column("rq_job_id", sa.String(), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_clips_video_id", "clips", ["video_id"])
    op.create_index("ix_clips_user_id", "clips", ["user_id"])
    op.create_index("ix_clips_status", "clips", ["status"])

    op.create_table(
        "clip_styles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("clip_id", sa.Integer(), nullable=False),
        sa.Column("caption_enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("caption_font", sa.String(), server_default="Inter", nullable=False),
        sa.Column("caption_size", sa.Integer(), server_default="56", nullable=False),
        sa.Column("caption_color", sa.String(), server_default="#FFFFFF", nullable=False),
        sa.Column("caption_bg_color", sa.String(), nullable=True),
        sa.Column("caption_position", sa.String(), server_default="bottom", nullable=False),
        sa.Column("caption_animation", sa.String(), nullable=True),
        sa.Column("caption_max_words", sa.Integer(), server_default="4", nullable=False),
        sa.Column("caption_words_per_line", sa.Integer(), server_default="3", nullable=False),
        sa.Column("caption_max_lines", sa.Integer(), server_default="2", nullable=False),
        sa.Column("caption_highlight_color", sa.String(), server_default="#FACC15", nullable=False),
        sa.Column("caption_highlight_style", sa.String(), server_default="color", nullable=False),
        sa.Column("caption_stroke_color", sa.String(), server_default="#000000", nullable=True),
        sa.Column("caption_stroke_width", sa.Integer(), server_default="3", nullable=False),
        sa.Column("caption_font_weight", sa.String(), server_default="700", nullable=False),
        sa.Column("caption_position_y", sa.Float(), server_default="85", nullable=True),
        sa.Column("caption_position_x", sa.Float(), server_default="50", nullable=True),
        sa.Column("caption_uppercase", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("brand_logo_url", sa.String(), nullable=True),
        sa.Column("brand_logo_position", sa.String(), server_default="top-right", nullable=True),
        sa.Column("brand_logo_scale", sa.Float(), server_default="0.12", nullable=False),
        sa.Column("brand_watermark_opacity", sa.Float(), server_default="0.85", nullable=False),
        sa.Column("background_music_url", sa.String(), nullable=True),
        sa.Column("background_music_volume", sa.Float(), server_default="0.25", nullable=False),
        sa.Column("original_audio_volume", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("video_keyframes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["clip_id"], ["clips.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("clip_id", name="uq_clip_styles_clip_id"),
    )
    op.create_index("ix_clip_styles_clip_id", "clip_styles", ["clip_id"])

    op.create_table(
        "clip_templates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("category", sa.String(), server_default="social", nullable=False),
        sa.Column("is_public", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("preview_url", sa.String(), nullable=True),
        sa.Column("style_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("usage_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_clip_templates_user_id", "clip_templates", ["user_id"])

    # Seed built-in global templates (user_id NULL)
    op.execute(
        """
        INSERT INTO clip_templates (user_id, name, category, is_public, style_config, usage_count)
        VALUES
        (NULL, 'Clean & Bold', 'social', true, '{
            "caption_enabled": true,
            "caption_font": "Inter",
            "caption_size": 60,
            "caption_color": "#FFFFFF",
            "caption_position": "bottom",
            "caption_highlight_color": "#FACC15",
            "caption_highlight_style": "color",
            "caption_stroke_color": "#000000",
            "caption_stroke_width": 4,
            "caption_font_weight": "800",
            "caption_position_y": 82,
            "caption_uppercase": true,
            "caption_words_per_line": 3,
            "caption_max_lines": 2
        }'::jsonb, 0),
        (NULL, 'Podcast Neon', 'podcast', true, '{
            "caption_enabled": true,
            "caption_font": "Inter",
            "caption_size": 56,
            "caption_color": "#22D3EE",
            "caption_position": "center",
            "caption_highlight_color": "#F472B6",
            "caption_highlight_style": "background",
            "caption_stroke_color": "#0F172A",
            "caption_stroke_width": 3,
            "caption_font_weight": "700",
            "caption_position_y": 55,
            "caption_words_per_line": 4,
            "caption_max_lines": 2
        }'::jsonb, 0),
        (NULL, 'Educational', 'educational', true, '{
            "caption_enabled": true,
            "caption_font": "Inter",
            "caption_size": 48,
            "caption_color": "#FFFFFF",
            "caption_position": "bottom",
            "caption_highlight_color": "#34D399",
            "caption_highlight_style": "underline",
            "caption_stroke_color": "#111827",
            "caption_stroke_width": 2,
            "caption_font_weight": "600",
            "caption_position_y": 88,
            "caption_words_per_line": 5,
            "caption_max_lines": 2
        }'::jsonb, 0)
        """
    )


def downgrade() -> None:
    op.drop_index("ix_clip_templates_user_id", table_name="clip_templates")
    op.drop_table("clip_templates")
    op.drop_index("ix_clip_styles_clip_id", table_name="clip_styles")
    op.drop_table("clip_styles")
    op.drop_index("ix_clips_status", table_name="clips")
    op.drop_index("ix_clips_user_id", table_name="clips")
    op.drop_index("ix_clips_video_id", table_name="clips")
    op.drop_table("clips")
