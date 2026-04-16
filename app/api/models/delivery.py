from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


class DeliveryPackageCreate(BaseModel):
    video_id: int
    approved_version_id: Optional[int] = None


class DeliveryPackageResponse(BaseModel):
    id: int
    project_id: int
    video_id: int
    status: str
    zip_url: Optional[str] = None
    zip_size_bytes: Optional[int] = None
    checksum_sha256: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None
    updated_at: datetime

    class Config:
        orm_mode = True


class DeliveryAssetResponse(BaseModel):
    id: int
    delivery_package_id: int
    asset_type: str
    file_url: str
    filename: str
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    checksum_sha256: Optional[str] = None
    created_at: datetime

    class Config:
        orm_mode = True


class DeliveryPackageDetailResponse(DeliveryPackageResponse):
    assets: list[DeliveryAssetResponse]


class DeliveryLinkCreate(BaseModel):
    expires_in_days: int = 30


class DeliveryLinkResponse(BaseModel):
    id: int
    delivery_package_id: int
    token: str
    expires_at: datetime
    renew_count: int
    is_revoked: bool
    created_at: datetime

    class Config:
        orm_mode = True


class DeliveryPublicInfo(BaseModel):
    token: str
    package: DeliveryPackageDetailResponse
    workspace_branding: Optional[dict[str, Any]] = None
    expires_at: datetime
    expired: bool


class DeliveryRenewRequest(BaseModel):
    extend_days: int = 30
