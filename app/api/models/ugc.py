"""Request schemas for the AI UGC API. Responses are serialized as plain dicts."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class ProductCreate(BaseModel):
    url: str
    workspace_id: Optional[int] = None


class BriefRequest(BaseModel):
    # Reserved for future tuning (tone, language); empty for now.
    pass


class CampaignCreate(BaseModel):
    product_id: int
    brief_id: Optional[int] = None
    name: Optional[str] = None
    platform: Optional[str] = "tiktok"
    # When omitted, resolved from the platform preset (ugc_platforms).
    aspect_ratio: Optional[str] = None
    length_sec: Optional[int] = None


class GenerateRequest(BaseModel):
    count: int = Field(default=10, ge=1, le=100)
    dimensions: Optional[dict[str, Any]] = None


class RegenerateRequest(BaseModel):
    hook: Optional[str] = None
    script: Optional[str] = None
    cta: Optional[str] = None
    angle: Optional[str] = None
    provider_avatar_id: Optional[str] = None
    provider_voice_id: Optional[str] = None
    aspect_ratio: Optional[str] = None
    length_sec: Optional[int] = None


class PerformanceCreate(BaseModel):
    source: Optional[str] = "manual"
    spend: Optional[float] = None
    impressions: Optional[int] = None
    clicks: Optional[int] = None
    ctr: Optional[float] = None
    conversions: Optional[int] = None
    cvr: Optional[float] = None
    roas: Optional[float] = None
    notes: Optional[str] = None
