"""Pydantic schemas for Editor Integration endpoints (NLE sync, proxies, watch folders, ingest)."""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


# ── NLE Sessions ──────────────────────────────────────────────────────


class NLESessionCreate(BaseModel):
    project_id: int
    nle_type: str = Field(..., pattern=r"^(premiere|resolve|fcpx|after_effects)$")
    nle_version: Optional[str] = None
    host_name: Optional[str] = None
    sync_direction: str = Field("bidirectional", pattern=r"^(push|pull|bidirectional)$")


class NLESessionResponse(BaseModel):
    id: int
    user_id: int
    project_id: int
    nle_type: str
    nle_version: Optional[str]
    host_name: Optional[str]
    last_sync_at: Optional[datetime]
    sync_direction: str
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


# ── Markers (comment ↔ NLE marker interchange) ───────────────────────


class MarkerItem(BaseModel):
    """One NLE marker — maps to one Editube comment."""

    timecode_sec: int
    end_timecode_sec: Optional[int] = None
    text: str
    author: Optional[str] = None
    color: Optional[str] = None  # NLE marker colour label
    kind: str = Field("comment", pattern=r"^(comment|change_request)$")
    status: Optional[str] = None
    editube_comment_id: Optional[int] = None  # set on export so NLE can match


class MarkerExportResponse(BaseModel):
    video_id: int
    marker_count: int
    markers: List[MarkerItem]
    exported_at: datetime


class MarkerImportRequest(BaseModel):
    markers: List[MarkerItem]
    source_nle: str = Field(..., pattern=r"^(premiere|resolve|fcpx|after_effects|generic)$")
    replace_existing: bool = False


class MarkerImportResponse(BaseModel):
    video_id: int
    created: int
    updated: int
    skipped: int
    total: int


class MarkerDiffResponse(BaseModel):
    video_id: int
    added: List[MarkerItem]
    removed: List[MarkerItem]
    changed: List[MarkerItem]
    since: datetime


# ── Proxy ─────────────────────────────────────────────────────────────


class ProxyRequest(BaseModel):
    profile: str = Field("540p_h264", pattern=r"^(540p_h264|720p_h264|1080p_h264)$")


class ProxyResponse(BaseModel):
    id: int
    video_id: int
    profile: str
    status: str
    width: Optional[int]
    height: Optional[int]
    bitrate_kbps: Optional[int]
    codec: Optional[str]
    file_url: Optional[str]
    size_bytes: Optional[int]
    duration: Optional[int]
    error_message: Optional[str]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ProxyListResponse(BaseModel):
    video_id: int
    proxies: List[ProxyResponse]


# ── Watch Folder ──────────────────────────────────────────────────────


class WatchFolderCreate(BaseModel):
    project_id: int
    folder_path: str
    auto_proxy: bool = True
    auto_version: bool = True
    file_pattern: str = "*.mp4,*.mov,*.mxf"


class WatchFolderUpdate(BaseModel):
    folder_path: Optional[str] = None
    auto_proxy: Optional[bool] = None
    auto_version: Optional[bool] = None
    file_pattern: Optional[str] = None
    is_active: Optional[bool] = None


class WatchFolderResponse(BaseModel):
    id: int
    user_id: int
    project_id: int
    folder_path: str
    auto_proxy: bool
    auto_version: bool
    file_pattern: str
    last_sync_at: Optional[datetime]
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class WatchFolderFileEntry(BaseModel):
    filename: str
    size_bytes: int
    modified_at: datetime
    checksum_sha256: Optional[str] = None


class WatchFolderSyncRequest(BaseModel):
    """Sent by the desktop agent with a list of detected files."""

    files: List[WatchFolderFileEntry]


class WatchFolderSyncResponse(BaseModel):
    config_id: int
    new_files: List[str]
    skipped_files: List[str]
    upload_urls: List[str]  # pre-signed or direct upload URLs for new files


# ── Camera-to-Cloud Ingest ────────────────────────────────────────────


class IngestUploadMeta(BaseModel):
    project_id: int
    name: str
    description: Optional[str] = None
    folder_id: Optional[int] = None
    device_name: Optional[str] = None
    location: Optional[str] = None
    auto_proxy: bool = True


class IngestStatusResponse(BaseModel):
    video_id: int
    video_name: str
    version: int
    file_url: str
    proxy_status: Optional[str] = None
    proxy_url: Optional[str] = None
    created_at: datetime
