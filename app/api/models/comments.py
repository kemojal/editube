from pydantic import BaseModel
from datetime import datetime
from typing import Optional, List, Any


class CommentBase(BaseModel):
    text: str
    timecode: int
    end_timecode: Optional[int] = None
    drawing_data: Optional[Any] = None  # serialized FabricJS objects or pin data

class CommentCreate(CommentBase):
    parent_id: Optional[int] = None
    is_private: bool = False

class CommentUpdate(BaseModel):
    text: Optional[str] = None
    is_resolved: Optional[bool] = None


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
    user: CommentUserResponse
    likes_count: int = 0
    liked_by_me: bool = False
    replies_count: int = 0
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True


class CommentWithRepliesResponse(CommentResponse):
    replies: List[CommentResponse] = []
