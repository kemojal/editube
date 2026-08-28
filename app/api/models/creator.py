from pydantic import BaseModel
from datetime import datetime
from typing import Optional, Any, List


# --- Publications ---

class PublicationCreate(BaseModel):
    platform: str
    title: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[str] = None
    category: Optional[str] = None
    privacy: Optional[str] = "private"
    scheduled_at: Optional[datetime] = None
    thumbnail_variant_id: Optional[int] = None
    extra: Optional[Any] = None


class PublicationUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[str] = None
    category: Optional[str] = None
    privacy: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    status: Optional[str] = None
    thumbnail_variant_id: Optional[int] = None
    extra: Optional[Any] = None


class PublicationResponse(BaseModel):
    id: int
    video_id: int
    platform: str
    status: str
    title: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[str] = None
    category: Optional[str] = None
    privacy: str
    scheduled_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    external_id: Optional[str] = None
    external_url: Optional[str] = None
    thumbnail_variant_id: Optional[int] = None
    extra: Optional[Any] = None
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- Aspect exports ---

class AspectExportCreate(BaseModel):
    aspect_ratio: str
    platform_preset: Optional[str] = None
    subject_tracking: bool = True


class AspectExportResponse(BaseModel):
    id: int
    video_id: int
    aspect_ratio: str
    platform_preset: Optional[str] = None
    status: str
    output_path: Optional[str] = None
    thumbnail_url: Optional[str] = None
    duration: Optional[int] = None
    subject_tracking: bool
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DeliveryExportCreate(BaseModel):
    profile_keys: Optional[List[str]] = None


class DeliveryExportResponse(BaseModel):
    id: int
    video_id: int
    profile_key: str
    status: str
    output_path: Optional[str] = None
    mime_type: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    size_bytes: Optional[int] = None
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- Chapters ---

class ChapterCreate(BaseModel):
    start_time: int
    end_time: Optional[int] = None
    title: str
    source: Optional[str] = "manual"
    order_index: Optional[int] = 0


class ChapterUpdate(BaseModel):
    start_time: Optional[int] = None
    end_time: Optional[int] = None
    title: Optional[str] = None
    order_index: Optional[int] = None


class ChapterResponse(BaseModel):
    id: int
    video_id: int
    start_time: int
    end_time: Optional[int] = None
    title: str
    source: str
    order_index: int
    created_at: datetime

    model_config = {"from_attributes": True}


class YoutubeChapterBlockResponse(BaseModel):
    """Timestamp lines for YouTube description (chapters)."""

    block: str


# --- End screens ---

class EndScreenBody(BaseModel):
    cards: Optional[Any] = None
    pinned_comment: Optional[str] = None


class EndScreenResponse(BaseModel):
    id: int
    video_id: int
    cards: Optional[Any] = None
    pinned_comment: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- Brand deals ---

class BrandDealCreate(BaseModel):
    video_id: Optional[int] = None
    sponsor_name: str
    contact_email: Optional[str] = None
    amount_cents: int = 0
    currency: str = "USD"
    segment_start: Optional[int] = None
    segment_end: Optional[int] = None
    integration_notes: Optional[str] = None


class BrandDealUpdate(BaseModel):
    video_id: Optional[int] = None
    sponsor_name: Optional[str] = None
    contact_email: Optional[str] = None
    amount_cents: Optional[int] = None
    currency: Optional[str] = None
    segment_start: Optional[int] = None
    segment_end: Optional[int] = None
    integration_notes: Optional[str] = None
    payout_status: Optional[str] = None


class BrandDealResponse(BaseModel):
    id: int
    project_id: int
    video_id: Optional[int] = None
    sponsor_name: str
    contact_email: Optional[str] = None
    amount_cents: int
    currency: str
    segment_start: Optional[int] = None
    segment_end: Optional[int] = None
    integration_notes: Optional[str] = None
    payout_status: str
    paid_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- Thumbnails ---

class ThumbnailVariantCreate(BaseModel):
    label: Optional[str] = None
    image_url: str


class ThumbnailVariantUpdate(BaseModel):
    label: Optional[str] = None
    is_winner: Optional[bool] = None
    impressions: Optional[int] = None
    clicks: Optional[int] = None


class ThumbnailVariantResponse(BaseModel):
    id: int
    video_id: int
    label: Optional[str] = None
    image_url: str
    is_winner: bool
    impressions: int
    clicks: int
    created_at: datetime

    model_config = {"from_attributes": True}
