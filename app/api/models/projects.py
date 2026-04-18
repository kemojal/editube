from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime


class WorkspaceAssetLinkCreate(BaseModel):
    workspace_asset_id: int
    folder_id: Optional[int] = None


class WorkspaceAssetLinkResponse(BaseModel):
    id: int
    project_id: int
    workspace_asset_id: int
    folder_id: Optional[int] = None
    category: str
    title: str
    file_url: str
    created_at: Optional[datetime] = None

    class Config:
        orm_mode = True

class ProjectBase(BaseModel):
    name: str
    description: str | None = None

class ProjectCreate(ProjectBase):
    workspace_id: Optional[int] = None
    template_key: Optional[str] = None

class ProjectUpdate(ProjectBase):
    pass

class ProjectCollaboratorCreate(BaseModel):
    user_id: int
    role: str

class ProjectCollaboratorUpdate(BaseModel):
    role: str

class CollaboratorEmailList(BaseModel):
    collaborator_emails: list[str]
    collaborator_roles: Optional[dict[str, str]] = None  # lowercased email -> role


class UserResponse(BaseModel):
    id: int
    name: str
    email: str
    avatar_url: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True

class ProjectResponse(BaseModel):
    id: int
    name: str
    description: str | None = None
    workspace_id: int
    created_at: datetime
    updated_at: datetime
    creator: UserResponse
    collaborators: List[UserResponse]

    class Config:
        orm_mode = True
