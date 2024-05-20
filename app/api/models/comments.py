from pydantic import BaseModel

class CommentBase(BaseModel):
    text: str
    timecode: int

class CommentCreate(CommentBase):
    pass

class CommentUpdate(CommentBase):
    pass