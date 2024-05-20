from pydantic import BaseModel

class VideoBase(BaseModel):
    name: str
    description: str | None = None

class VideoCreate(VideoBase):
    name: str
    description: str | None = None
    pass

class VideoUpdate(VideoBase):
    pass