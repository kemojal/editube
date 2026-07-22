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


class YoutubeVideoCreate(BaseModel):
    """POST /projects/{project_id}/videos/youtube — create a source video from a YouTube URL."""

    url: str
    name: Optional[str] = None
    # Spoken language for transcription, ISO 639-1 (e.g. "en"). "auto"/""/absent = auto-detect.
    language: Optional[str] = None


class VideoFromUploadCreate(BaseModel):
    """POST /projects/{project_id}/videos/from-upload — register an already-uploaded
    file (via stateless POST /upload/video) as a project video."""

    file_path: str
    name: str
    description: Optional[str] = None
    folder_id: Optional[int] = None
    # Spoken language for transcription, ISO 639-1 (e.g. "en"). "auto"/""/absent = auto-detect.
    language: Optional[str] = None
    size_bytes: Optional[int] = None


class VideoTranscriptionNested(BaseModel):
    """Transcript row for a video (from video_transcriptions table)."""

    status: str
    segments: Optional[List[Any]] = None
    speakers: Optional[List[str]] = None
    speaker_count: Optional[int] = None
    error_message: Optional[str] = None
    updated_at: Optional[datetime] = None
    # User-requested spoken language (ISO 639-1). None = auto-detect.
    language: Optional[str] = None
    # Language Whisper actually detected (persisted even in auto-detect mode).
    detected_language: Optional[str] = None

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
    thumbnail_url: str | None = None
    file_path: str | None = None
    duration: int | None = None
    comment_count: int = 0
    uploader_name: str | None = None

    class Config:
        orm_mode = True


VideoWithProjectResponse.model_rebuild()
