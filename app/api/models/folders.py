from pydantic import BaseModel
from datetime import datetime
from typing import Optional, List


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
    file_path: Optional[str] = None
    thumbnail_url: Optional[str] = None
    project_id: int
    folder_id: Optional[int] = None
    uploader_id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True


class ProjectContentsResponse(BaseModel):
    folders: List[FolderResponse]
    videos: List[VideoResponse]
    breadcrumb: List[FolderResponse]
    total_items: int
