from pydantic import BaseModel
from datetime import datetime
from typing import Optional, Any, List


class AnnotationCreate(BaseModel):
    annotation_type: str  # "fabric_object" — the shape type
    annotation_data: Any  # Full FabricJS serialized object (nested dict)
    timecode: int  # Video second when this annotation was placed
    duration: Optional[int] = 5  # How long (seconds) to display; default 5s
    is_private: bool = False


class AnnotationUpdate(BaseModel):
    annotation_data: Optional[Any] = None
    timecode: Optional[int] = None
    duration: Optional[int] = None


class AnnotationUserResponse(BaseModel):
    id: int
    name: str
    email: str

    class Config:
        orm_mode = True


class AnnotationResponse(BaseModel):
    id: int
    video_id: int
    user: AnnotationUserResponse
    annotation_type: str
    annotation_data: Any
    timecode: int
    duration: int
    is_private: bool = False
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True
