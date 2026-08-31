from pydantic import BaseModel, Field
from datetime import datetime
from typing import Literal, Optional, List, Any


class VideoBase(BaseModel):
    name: str
    description: str | None = None

class VideoCreate(VideoBase):
    pass

class VideoUpdate(VideoBase):
    pass


class VideoStatusUpdate(BaseModel):
    status: str  # in_progress, in_review, approved, needs_changes


class SendForReviewRequest(BaseModel):
    """POST /projects/{project_id}/videos/{video_id}/send-for-review."""

    # Empty means "whoever owns the work" — a solo creator should not have to
    # nominate themselves as a reviewer to send their own cut out.
    reviewer_user_ids: Optional[List[int]] = None
    due_at: Optional[datetime] = None
    note: Optional[str] = None


class ReviewDecisionRequest(BaseModel):
    """POST /videos/{video_id}/review-decision."""

    decision: Literal["approved", "changes_requested"]
    note: Optional[str] = None
    # "Approve with notes": sign off despite open change requests. Clients
    # approve with two nits outstanding constantly, and forcing a fake extra
    # round to express that is worse than recording it honestly.
    override_blockers: bool = False


class ApprovalBlockerResponse(BaseModel):
    code: str
    message: str
    count: Optional[int] = None


class VideoDecisionSummary(BaseModel):
    decision: str
    actor_name: Optional[str] = None
    note: Optional[str] = None
    created_at: Optional[datetime] = None
    superseded: bool = False


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
    # Trusted attribution for a completed Google Drive import. The API verifies
    # ownership, terminal status, and the storage URL before accepting it.
    drive_import_id: Optional[int] = None
    # When set, this registration becomes the next version in that video's
    # chain — same semantics as the multipart route's `version_of` form field,
    # so the resumable browser-to-storage path gets carry-forward, approval
    # superseding, and new-version notifications for free.
    version_of: Optional[int] = None
    version_notes: Optional[str] = None


class VideoTranscriptionNested(BaseModel):
    """Transcript row for a video (from video_transcriptions table)."""

    status: str
    segments: Optional[List[Any]] = None
    speakers: Optional[List[str]] = None
    speaker_count: Optional[int] = None
    # Silero-VAD speech/silence ranges over the source audio (see
    # VideoTranscription.audio_analysis). None for pre-existing transcripts.
    audio_analysis: Optional[dict] = None
    error_message: Optional[str] = None
    updated_at: Optional[datetime] = None
    # User-requested spoken language (ISO 639-1). None = auto-detect.
    language: Optional[str] = None
    # Language Whisper actually detected (persisted even in auto-detect mode).
    detected_language: Optional[str] = None

    model_config = {"from_attributes": True}


class UploaderResponse(BaseModel):
    id: int
    name: str
    email: str
    avatar_url: str | None = None

    model_config = {"from_attributes": True}


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
    # Null on rows that predate the status spine — we genuinely do not know
    # when those last moved, and inventing a timestamp would be a lie.
    status_changed_at: Optional[datetime] = None
    review_due_at: Optional[datetime] = None
    version_notes: Optional[str] = None
    decision: Optional[VideoDecisionSummary] = None
    duration: int | None = None
    uploader: UploaderResponse
    created_at: datetime
    updated_at: datetime
    comments_count: int = 0
    annotations_count: int = 0
    transcription: Optional[VideoTranscriptionNested] = None
    # True when the viewer may moderate others' comments (workflow status, assignee, etc.).
    can_moderate: bool = False
    # Editing-proxy rendition (default profile). `proxy_url` is set only when
    # generation completed; `proxy_status` is pending|processing|completed|
    # failed, or null when no proxy row exists. Playback should prefer the
    # proxy and fall back to `file_path`; export always uses `file_path`.
    proxy_url: str | None = None
    proxy_status: str | None = None

    model_config = {"from_attributes": True}


class ProjectSummary(BaseModel):
    id: int
    name: str
    description: str | None = None

    model_config = {"from_attributes": True}


class VideoWithProjectResponse(VideoDetailResponse):
    project: ProjectSummary
    versions: List["VideoVersionSummary"] = []


class VideoEditorBootstrapResponse(VideoWithProjectResponse):
    """One-round-trip editor open.

    The rough-cut draft rides along with the detail payload instead of
    costing a second sequential request — against a high-latency link the
    detail → draft waterfall alone was multiple seconds.
    """

    draft: dict | None = None


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
    # Lets the version switcher answer "which cut was actually approved?"
    # without a request per version.
    status: str = "in_progress"

    model_config = {"from_attributes": True}


VideoWithProjectResponse.model_rebuild()
