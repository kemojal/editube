from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# --- Suggestions ---------------------------------------------------------


class SuggestClipsRequest(BaseModel):
    min_duration: float = Field(15.0, ge=5.0, le=120.0)
    max_duration: float = Field(60.0, ge=10.0, le=180.0)
    max_suggestions: int = Field(8, ge=1, le=20)


class ClipSuggestion(BaseModel):
    start_time: float
    end_time: float
    duration: float
    virality_score: float
    reason: str
    transcript: str
    hooks_matched: list[str]


class SuggestClipsResponse(BaseModel):
    suggestions: list[ClipSuggestion]
    transcription_ready: bool
    video_duration: float | None = None


# --- Clips ---------------------------------------------------------------


class ClipStyleOut(BaseModel):
    caption_enabled: bool
    caption_font: str
    caption_size: int
    caption_color: str
    caption_bg_color: str | None = None
    caption_position: str
    caption_animation: str | None = None
    caption_max_words: int
    caption_words_per_line: int
    caption_max_lines: int
    caption_highlight_color: str
    caption_highlight_style: str
    caption_stroke_color: str | None = None
    caption_stroke_width: int
    caption_font_weight: str
    caption_position_y: float | None = None
    caption_position_x: float | None = None
    caption_uppercase: bool
    caption_template_id: str | None = None
    brand_logo_url: str | None = None
    brand_logo_position: str | None = None
    brand_logo_scale: float
    brand_watermark_opacity: float
    background_music_url: str | None = None
    background_music_volume: float
    original_audio_volume: float
    video_keyframes: Any | None = None

    model_config = {"from_attributes": True}


class ClipStyleUpdate(BaseModel):
    caption_enabled: bool | None = None
    caption_font: str | None = None
    caption_size: int | None = Field(default=None, ge=12, le=200)
    caption_color: str | None = None
    caption_bg_color: str | None = None
    caption_position: str | None = None
    caption_animation: str | None = None
    caption_max_words: int | None = Field(default=None, ge=1, le=20)
    caption_words_per_line: int | None = Field(default=None, ge=1, le=10)
    caption_max_lines: int | None = Field(default=None, ge=1, le=5)
    caption_highlight_color: str | None = None
    caption_highlight_style: str | None = None
    caption_stroke_color: str | None = None
    caption_stroke_width: int | None = Field(default=None, ge=0, le=20)
    caption_font_weight: str | None = None
    caption_position_y: float | None = None
    caption_position_x: float | None = None
    caption_uppercase: bool | None = None
    caption_template_id: str | None = None
    brand_logo_url: str | None = None
    brand_logo_position: str | None = None
    brand_logo_scale: float | None = None
    brand_watermark_opacity: float | None = None
    background_music_url: str | None = None
    background_music_volume: float | None = None
    original_audio_volume: float | None = None
    video_keyframes: Any | None = None


class ClipRange(BaseModel):
    start: float = Field(..., ge=0)
    end: float = Field(..., gt=0)


class ClipTranscriptAuthor(BaseModel):
    user_id: int | None = None
    name: str
    avatar_url: str | None = None


class ClipTranscriptHighlight(BaseModel):
    id: str
    start: float = Field(..., ge=0)
    end: float = Field(..., gt=0)
    color: str = "yellow"
    start_segment_index: int | None = None
    start_word_index: int | None = None
    end_segment_index: int | None = None
    end_word_index: int | None = None
    anchor_text: str | None = None
    author: ClipTranscriptAuthor | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ClipTranscriptComment(BaseModel):
    id: str
    start: float = Field(..., ge=0)
    end: float = Field(..., gt=0)
    text: str
    start_segment_index: int | None = None
    start_word_index: int | None = None
    end_segment_index: int | None = None
    end_word_index: int | None = None
    anchor_text: str | None = None
    author: ClipTranscriptAuthor | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ClipEditHistoryEntry(BaseModel):
    id: str
    kind: str = "autosave"
    label: str | None = None
    author: ClipTranscriptAuthor | None = None
    name: str | None = None
    aspect_ratio: str | None = None
    cuts: list[ClipRange] = Field(default_factory=list)
    transcript_text: str | None = None
    transcript_highlights: list[ClipTranscriptHighlight] = Field(default_factory=list)
    transcript_comments: list[ClipTranscriptComment] = Field(default_factory=list)
    created_at: datetime | None = None


class ClipCreate(BaseModel):
    video_id: int
    name: str | None = None
    start_time: float = Field(..., ge=0)
    end_time: float = Field(..., gt=0)
    aspect_ratio: str = "9:16"
    virality_score: float | None = None
    is_ai_suggested: bool = False
    suggestion_reason: str | None = None
    hooks_matched: list[str] | None = None
    transcript_text: str | None = None
    cuts: list[ClipRange] | None = None
    transcript_highlights: list[ClipTranscriptHighlight] | None = None
    transcript_comments: list[ClipTranscriptComment] | None = None


class ClipUpdate(BaseModel):
    name: str | None = None
    start_time: float | None = Field(default=None, ge=0)
    end_time: float | None = Field(default=None, gt=0)
    aspect_ratio: str | None = None
    transcript_text: str | None = None
    cuts: list[ClipRange] | None = None
    transcript_highlights: list[ClipTranscriptHighlight] | None = None
    transcript_comments: list[ClipTranscriptComment] | None = None
    history_kind: str | None = None
    history_label: str | None = None


class ClipOut(BaseModel):
    id: int
    video_id: int
    user_id: int | None
    name: str
    start_time: float
    end_time: float
    cuts: list[ClipRange] = Field(default_factory=list)
    duration_seconds: float | None
    aspect_ratio: str
    virality_score: float | None
    status: str
    render_progress: int
    render_error: str | None
    storage_path: str | None
    thumbnail_url: str | None
    transcript_text: str | None
    transcript_highlights: list[ClipTranscriptHighlight] = Field(default_factory=list)
    transcript_comments: list[ClipTranscriptComment] = Field(default_factory=list)
    edit_history: list[ClipEditHistoryEntry] = Field(default_factory=list)
    is_ai_suggested: bool
    suggestion_reason: str | None
    hooks_matched: list[str] | None
    preset: str | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    style: ClipStyleOut | None = None

    model_config = {"from_attributes": True}


class ClipRenderRequest(BaseModel):
    preset: str | None = None


class ClipRenderResponse(BaseModel):
    clip_id: int
    status: str
    rq_job_id: str | None


# --- Templates -----------------------------------------------------------


class TemplateCreate(BaseModel):
    name: str
    category: str = "social"
    is_public: bool = False
    preview_url: str | None = None
    style_config: dict


class TemplateUpdate(BaseModel):
    name: str | None = None
    category: str | None = None
    is_public: bool | None = None
    preview_url: str | None = None
    style_config: dict | None = None


class TemplateOut(BaseModel):
    id: int
    user_id: int | None
    name: str
    category: str
    is_public: bool
    preview_url: str | None
    style_config: dict
    usage_count: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- Repurpose wizard -----------------------------------------------------


class YoutubeMetadataRequest(BaseModel):
    url: str


class YoutubeMetadataOut(BaseModel):
    url: str
    title: str | None = None
    thumbnail_url: str | None = None
    channel_title: str | None = None
    duration_seconds: int | None = None
    provider: str | None = None
    embed_url: str | None = None


class RepurposeJobCreate(BaseModel):
    source_mode: str = Field(pattern="^(youtube_url|upload|project_video)$")
    project_id: int | None = None
    video_id: int | None = None
    youtube_url: str | None = None
    source_file_url: str | None = None
    source_title: str | None = None
    source_meta: dict | None = None
    clip_mode: str = Field(default="basic", pattern="^(basic|clip_anything)$")
    clip_anything_prompt: str | None = None
    genres: list[str] = Field(default_factory=list)
    clip_length_bucket: str = "lt_30"
    subtitle_template_id: int | None = None
    aspect_ratio: str = "9:16"
    # Multi-aspect fan-out: when provided, wins over `aspect_ratio` and produces
    # one Clip per suggested moment per aspect ratio. Back-compat: omit/empty to
    # keep today's single-aspect-ratio behavior.
    aspect_ratios: list[Literal["9:16", "1:1", "16:9"]] | None = None
    # Number of suggested MOMENTS to clip (not total clips — total = clip_count *
    # len(aspect_ratios)). Defaults to 8 (pipeline's prior hardcoded value) when omitted.
    clip_count: int | None = Field(default=None, ge=1, le=20)
    source_range_start_seconds: float | None = Field(default=None, ge=0, le=43200)
    source_range_end_seconds: float | None = Field(default=None, ge=1, le=43200)
    source_trim_seconds: int | None = Field(default=None, ge=5, le=43200)
    # Spoken language for transcription seeding on youtube_url/upload sources
    # ("auto"/""/None = auto-detect, normalized via app.utils.language.normalize_language).
    language: str | None = None
    save_as_default: bool = False
    auto_start: bool = True


class RepurposeJobOut(BaseModel):
    id: int
    user_id: int
    project_id: int | None
    video_id: int | None
    source_mode: str
    source_url: str | None
    source_file_url: str | None
    source_title: str | None
    source_meta: dict | None
    clip_mode: str
    clip_anything_prompt: str | None
    genres: list[str]
    clip_length_bucket: str
    subtitle_template_id: int | None
    aspect_ratio: str
    aspect_ratios: list[str] | None = None
    clip_count: int | None = None
    source_range_start_seconds: float | None = None
    source_range_end_seconds: float | None = None
    source_trim_seconds: int | None
    status: str
    created_clip_ids: list[int] | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class RepurposeUserDefaultsUpdate(BaseModel):
    clip_mode: str = Field(default="basic", pattern="^(basic|clip_anything)$")
    default_prompt: str | None = None
    genres: list[str] = Field(default_factory=list)
    clip_length_bucket: str = "lt_30"
    subtitle_template_id: int | None = None
    aspect_ratio: str = "9:16"
    source_trim_seconds: int | None = Field(default=None, ge=5, le=7200)


class RepurposeUserDefaultsOut(BaseModel):
    clip_mode: str
    default_prompt: str | None = None
    genres: list[str]
    clip_length_bucket: str
    subtitle_template_id: int | None = None
    aspect_ratio: str
    source_trim_seconds: int | None = None
