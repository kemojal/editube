from pydantic import BaseModel
from datetime import datetime
from typing import Optional, List


class ContributorInfo(BaseModel):
    id: Optional[int] = None
    name: str
    avatar_url: Optional[str] = None

    class Config:
        orm_mode = True


class FolderCreate(BaseModel):
    name: str
    parent_id: Optional[int] = None


class FolderUpdate(BaseModel):
    name: Optional[str] = None
    parent_id: Optional[int] = None


class FolderResponse(BaseModel):
    id: int
    name: str
    project_id: int
    parent_id: Optional[int] = None
    created_by: int
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True


class VideoResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    version: int
    version_group_id: Optional[str] = None
    file_path: Optional[str] = None
    thumbnail_url: Optional[str] = None
    project_id: int
    folder_id: Optional[int] = None
    uploader_id: int
    created_at: datetime
    updated_at: datetime
    comment_count: int = 0
    task_count: int = 0
    contributors: List[ContributorInfo] = []

    class Config:
        orm_mode = True


class ProjectContentsResponse(BaseModel):
    folders: List[FolderResponse]
    videos: List[VideoResponse]
    breadcrumb: List[FolderResponse]
    total_items: int
