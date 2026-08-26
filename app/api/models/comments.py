from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List, Any, Literal


class CommentBase(BaseModel):
    text: str
    timecode: int
    end_timecode: Optional[int] = None
    drawing_data: Optional[Any] = None  # serialized FabricJS objects or pin data
    transcript_segment_index: Optional[int] = None
    word_start_index: Optional[int] = None
    word_end_index: Optional[int] = None
    anchor_text: Optional[str] = None


class CommentCreate(CommentBase):
    parent_id: Optional[int] = None
    is_private: bool = False
    visibility: Optional[Literal["public", "team", "author_only"]] = None
    due_at: Optional[datetime] = None
    kind: Literal["comment", "change_request"] = "comment"
    client_mutation_id: Optional[str] = None


class CommentUpdate(BaseModel):
    text: Optional[str] = None
    is_resolved: Optional[bool] = None
    status: Optional[
        Literal["open", "in_progress", "resolved", "wontfix", "reopened"]
    ] = None
    assignee_user_id: Optional[int] = None
    due_at: Optional[datetime] = None
    visibility: Optional[Literal["public", "team", "author_only"]] = None
    revision: Optional[int] = None


class CommentUserResponse(BaseModel):
    id: int
    name: str
    email: str
    avatar_url: str | None = None

    class Config:
        orm_mode = True


class CommentResponse(BaseModel):
    id: int
    video_id: int
    parent_id: int | None = None
    text: str
    timecode: int
    end_timecode: int | None = None
    drawing_data: Any | None = None
    transcript_segment_index: int | None = None
    word_start_index: int | None = None
    word_end_index: int | None = None
    anchor_text: str | None = None
    transcript_anchor_resolved: bool = True
    transcript_anchor_reason: str | None = None
    transcript_anchor_remap_timecode: int | None = None
    is_resolved: bool = False
    is_private: bool = False
    visibility: str = "public"
    due_at: Optional[datetime] = None
    kind: str = "comment"
    status: str = "open"
    assignee: Optional[CommentUserResponse] = None
    user: Optional[CommentUserResponse] = None
    guest_name: Optional[str] = None
    guest_email: Optional[str] = None
    guest_avatar_url: Optional[str] = None
    review_link_id: Optional[int] = None
    client_mutation_id: Optional[str] = None
    revision: int = 1
    # Points at the original on the previous version when this change request
    # was carried forward.
    carried_from_comment_id: Optional[int] = None
    attachments: List[dict] = Field(default_factory=list)
    likes_count: int = 0
    liked_by_me: bool = False
    replies_count: int = 0
    # Version-history fields (set when listing with include_prior): a comment
    # from an older version in the chain is read-only and carries its version.
    read_only: bool = False
    source_version: int | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True


class CommentWithRepliesResponse(CommentResponse):
    replies: List[CommentResponse] = Field(default_factory=list)


class CommentBulkAction(BaseModel):
    action: Literal["resolve", "set_status", "set_assignee"]
    set_status: Optional[
        Literal["open", "in_progress", "resolved", "wontfix", "reopened"]
    ] = None
    assignee_user_id: Optional[int] = None
    only_kind: Optional[Literal["comment", "change_request"]] = None
    only_top_level: bool = True
    max_rows: int = Field(default=500, le=2000)


class CommentAttachmentCreate(BaseModel):
    attachment_type: Literal["voice_note", "image", "file"] = "voice_note"
    file_url: str
    mime_type: Optional[str] = None
    duration_ms: Optional[int] = None
    bytes_size: Optional[int] = None
    waveform: Optional[Any] = None
    transcript: Optional[str] = None


class CommentSyncOperation(BaseModel):
    operation_id: str
    action: Literal["create", "update", "delete"]
    comment_id: Optional[int] = None
    payload: Optional[dict] = None


class CommentSyncRequest(BaseModel):
    operations: List[CommentSyncOperation] = Field(default_factory=list)


class CommentSyncResult(BaseModel):
    operation_id: str
    ok: bool
    comment_id: Optional[int] = None
    error: Optional[str] = None


class CommentSyncResponse(BaseModel):
    results: List[CommentSyncResult] = Field(default_factory=list)
