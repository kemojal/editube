from pydantic import BaseModel
from datetime import datetime

class CommentBase(BaseModel):
    text: str
    timecode: int

class CommentCreate(CommentBase):
    pass

class CommentUpdate(CommentBase):
    pass



class UserResponse(BaseModel):
    id: int
    name: str
    email: str
    role: str

    class Config:
        orm_mode = True


class CommentResponse(CommentBase):
    id: int
    video_id: int
    user: UserResponse
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True
