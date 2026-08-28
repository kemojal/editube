from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class SuggestionUserResponse(BaseModel):
    id: int
    name: str
    email: str
    avatar_url: str | None = None

    model_config = {"from_attributes": True}


class SuggestionCreate(BaseModel):
    title: str
    body: str
    category: Optional[str] = None


class SuggestionCommentCreate(BaseModel):
    body: str


class SuggestionCommentResponse(BaseModel):
    id: int
    suggestion_id: int
    body: str
    user: SuggestionUserResponse
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SuggestionResponse(BaseModel):
    id: int
    title: str
    body: str
    category: Optional[str] = None
    status: str
    upvotes_count: int
    comments_count: int
    voted_by_me: bool = False
    user: SuggestionUserResponse
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
