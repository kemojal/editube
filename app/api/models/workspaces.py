from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class WorkspaceMemberResponse(BaseModel):
    user_id: int
    role: str
    name: Optional[str] = None
    email: Optional[str] = None
    avatar_url: Optional[str] = None
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


class WorkspaceInviteCreatedResponse(BaseModel):
    token: str
    expires_at: datetime
    email_sent: bool


class WorkspaceInviteListItem(BaseModel):
    id: int
    email: str
    role: str
    expires_at: datetime
    accepted_at: Optional[datetime] = None
    created_at: datetime
    status: str  # pending | accepted | expired


class MyPendingWorkspaceInviteItem(BaseModel):
    id: int
    workspace_id: int
    workspace_name: str
    email: str
    role: str
    token: str
    expires_at: datetime
    invited_by_name: Optional[str] = None
    created_at: datetime
    status: str  # pending


class WorkspaceProvisionMemberBody(BaseModel):
    """Workspace owner only: create a password account and add to this workspace, or add existing user."""

    email: str
    name: Optional[str] = None
    role: str = Field(default="editor")
    password: Optional[str] = None


class WorkspaceProvisionMemberResponse(BaseModel):
    created_new_user: bool
    email: str
    workspace_role: str
    temporary_password: Optional[str] = None
    email_sent: bool
    detail: Optional[str] = None


class WorkspaceBrandingUpdate(BaseModel):
    logo_url: Optional[str] = None
    primary_color: Optional[str] = None
    accent_color: Optional[str] = None
    client_footer_text: Optional[str] = None
    custom_domain: Optional[str] = None


class WorkspaceBrandingResponse(BaseModel):
    """Branding row for team settings (authenticated). Verification token only for roles that may edit."""

    logo_url: Optional[str] = None
    primary_color: Optional[str] = None
    accent_color: Optional[str] = None
    client_footer_text: Optional[str] = None
    custom_domain: Optional[str] = None
    domain_verified_at: Optional[datetime] = None
    domain_verification_token: Optional[str] = None


class WorkspaceAssetCreate(BaseModel):
    title: str
    category: str
    file_url: str
    extra: Optional[dict[str, Any]] = None


class WorkspaceAssetUpdate(BaseModel):
    title: Optional[str] = None
    category: Optional[str] = None


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


class WorkspaceSSOProviderCreate(BaseModel):
    provider: str  # google | okta | azure_ad
    issuer: str
    client_id: str
    client_secret: str
    domain_hint: Optional[str] = None
    enabled: bool = True


class WorkspaceSSOProviderResponse(BaseModel):
    id: int
    provider: str
    issuer: str
    client_id: str
    domain_hint: Optional[str] = None
    enabled: bool
    created_at: datetime
    updated_at: datetime


class WorkspaceAuthPolicyUpdate(BaseModel):
    enforce_sso: Optional[bool] = None
    allowed_login_methods: Optional[list[str]] = None
    mfa_required: Optional[bool] = None


class WorkspaceAuthPolicyResponse(BaseModel):
    workspace_id: int
    enforce_sso: bool
    allowed_login_methods: list[str] = []
    mfa_required: bool


class NDADocumentCreate(BaseModel):
    name: str
    version: str
    body_markdown: str
    is_active: bool = True


class NDADocumentResponse(BaseModel):
    id: int
    workspace_id: int
    name: str
    version: str
    body_markdown: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
