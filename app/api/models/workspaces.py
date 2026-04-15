from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class WorkspaceMemberResponse(BaseModel):
    user_id: int
    role: str
    name: Optional[str] = None
    email: Optional[str] = None
    created_at: Optional[datetime] = None


class WorkspaceSummaryResponse(BaseModel):
    id: int
    name: str
    slug: str
    owner_user_id: int
    role: str  # current user's role in this workspace


class WorkspaceUpdate(BaseModel):
    name: Optional[str] = None


class WorkspaceInviteCreate(BaseModel):
    email: str
    role: str = Field(default="editor")


class WorkspaceInviteAccept(BaseModel):
    token: str


class WorkspaceBrandingUpdate(BaseModel):
    logo_url: Optional[str] = None
    primary_color: Optional[str] = None
    accent_color: Optional[str] = None
    client_footer_text: Optional[str] = None
    custom_domain: Optional[str] = None


class WorkspaceAssetCreate(BaseModel):
    title: str
    category: str
    file_url: str
    extra: Optional[dict[str, Any]] = None


class ProjectTemplateResponse(BaseModel):
    id: int
    template_key: str
    name: str
    workspace_id: Optional[int] = None


class CapacityMemberRow(BaseModel):
    user_id: int
    name: Optional[str] = None
    email: Optional[str] = None
    role: str
    active_project_count: int
    tracked_hours: float
    open_assigned_comments: int
