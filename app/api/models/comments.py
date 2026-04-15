from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List, Any, Literal


class CommentBase(BaseModel):
    text: str
    timecode: int
    end_timecode: Optional[int] = None
    drawing_data: Optional[Any] = None  # serialized FabricJS objects or pin data


class CommentCreate(CommentBase):
    parent_id: Optional[int] = None
    is_private: bool = False
    visibility: Optional[Literal["public", "team", "author_only"]] = None
    due_at: Optional[datetime] = None
    kind: Literal["comment", "change_request"] = "comment"


class CommentUpdate(BaseModel):
    text: Optional[str] = None
    is_resolved: Optional[bool] = None
    status: Optional[
        Literal["open", "in_progress", "resolved", "wontfix", "reopened"]
    ] = None
    assignee_user_id: Optional[int] = None
    due_at: Optional[datetime] = None
    visibility: Optional[Literal["public", "team", "author_only"]] = None


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
    likes_count: int = 0
    liked_by_me: bool = False
    replies_count: int = 0
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
