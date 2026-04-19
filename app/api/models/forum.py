from pydantic import BaseModel, ConfigDict
from typing import Optional, List
from datetime import datetime

class UserBaseInfo(BaseModel):
    id: int
    name: Optional[str] = None
    avatar_url: Optional[str] = None
    
    model_config = ConfigDict(from_attributes=True)

class ForumCategoryBase(BaseModel):
    name: str
    slug: str
    description: Optional[str] = None
    color: Optional[str] = None

class ForumCategoryResponse(ForumCategoryBase):
    id: int
    created_at: datetime
    
    model_config = ConfigDict(from_attributes=True)

class ForumPostCreate(BaseModel):
    title: str
    content: str
    category_id: int

class ForumPostUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    category_id: Optional[int] = None
    status: Optional[str] = None

class ForumPostResponse(BaseModel):
    id: int
    title: str
    content: str
    status: str
    view_count: int
    user_id: Optional[int] = None
    category_id: int
    created_at: datetime
    updated_at: datetime
    
    user: Optional[UserBaseInfo] = None
    category: Optional[ForumCategoryResponse] = None
    upvotes_count: int = 0
    comments_count: int = 0
    has_upvoted: bool = False
    
    model_config = ConfigDict(from_attributes=True)

class ForumCommentCreate(BaseModel):
    content: str
    parent_id: Optional[int] = None

class ForumCommentResponse(BaseModel):
    id: int
    content: str
    post_id: int
    user_id: Optional[int] = None
    parent_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    
    user: Optional[UserBaseInfo] = None
    
    model_config = ConfigDict(from_attributes=True)

class ForumPostDetailResponse(ForumPostResponse):
    comments: List[ForumCommentResponse] = []
