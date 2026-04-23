from pydantic import BaseModel
from datetime import datetime
from typing import Optional, List, Any


class VideoBase(BaseModel):
    name: str
    description: str | None = None

class VideoCreate(VideoBase):
    pass

class VideoUpdate(VideoBase):
    pass


class VideoStatusUpdate(BaseModel):
    status: str  # in_progress, in_review, approved, needs_changes


class VideoTranscriptionNested(BaseModel):
    """Transcript row for a video (from video_transcriptions table)."""

    status: str
    segments: Optional[List[Any]] = None
    speakers: Optional[List[str]] = None
    speaker_count: Optional[int] = None
    error_message: Optional[str] = None
    updated_at: Optional[datetime] = None

    class Config:
        orm_mode = True


class UploaderResponse(BaseModel):
    id: int
    name: str
    email: str
    avatar_url: str | None = None

    class Config:
        orm_mode = True


class VideoDetailResponse(BaseModel):
    id: int
    project_id: int
    folder_id: int | None = None
    name: str
    description: str | None = None
    version: int
    file_path: str
    thumbnail_url: str | None = None
    status: str
    duration: int | None = None
    uploader: UploaderResponse
    created_at: datetime
    updated_at: datetime
    comments_count: int = 0
    annotations_count: int = 0
    transcription: Optional[VideoTranscriptionNested] = None
    # True when the viewer may moderate others' comments (workflow status, assignee, etc.).
    can_moderate: bool = False

    class Config:
        orm_mode = True


class ProjectSummary(BaseModel):
    id: int
    name: str
    description: str | None = None

    class Config:
        orm_mode = True


class VideoWithProjectResponse(VideoDetailResponse):
    project: ProjectSummary
    versions: List["VideoVersionSummary"] = []


class VideoVersionSummary(BaseModel):
    id: int
    version: int
    name: str
    created_at: datetime

    class Config:
        orm_mode = True


VideoWithProjectResponse.model_rebuild()
