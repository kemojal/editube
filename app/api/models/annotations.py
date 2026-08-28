from pydantic import BaseModel
from datetime import datetime
from typing import Optional, Any, List


ANNOTATION_DURATION_FRAMES = 3 / 30  # 3 frames at 30fps


class AnnotationCreate(BaseModel):
    annotation_type: str  # "fabric_object" — the shape type
    annotation_data: Any  # Full FabricJS serialized object (nested dict)
    timecode: float  # Video time (seconds) when this annotation was placed
    duration: Optional[float] = ANNOTATION_DURATION_FRAMES
    is_private: bool = False


class AnnotationUpdate(BaseModel):
    annotation_data: Optional[Any] = None
    timecode: Optional[float] = None
    duration: Optional[float] = None


class AnnotationUserResponse(BaseModel):
    id: int
    name: str
    email: str
    avatar_url: Optional[str] = None

    model_config = {"from_attributes": True}


class AnnotationResponse(BaseModel):
    id: int
    video_id: int
    user: AnnotationUserResponse
    annotation_type: str
    annotation_data: Any
    timecode: float
    duration: float
    is_private: bool = False
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
