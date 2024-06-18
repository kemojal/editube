from pydantic import BaseModel
from datetime import datetime
from typing import Optional, Dict
# from app.api.models.users import UserResponse

class AnnotationBase(BaseModel):
    annotation_type: str
    annotation_data: dict
    timecode: str

class AnnotationCreate(AnnotationBase):
    pass

class AnnotationUpdate(AnnotationBase):
    annotation_type: Optional[str] = None
    annotation_data: Optional[dict] = None
    timecode: Optional[str] = None



class UserResponse(BaseModel):
    id: int
    name: str
    email: str
    role: str

    class Config:
        orm_mode = True    
class AnnotationResponse(BaseModel):
    id: int
    video_id: int
    user: UserResponse  # Ensure `user` is correctly populated as `UserResponse`
    annotation_data: Optional[Dict[str, str]] = None  # Adjust types as necessary
    annotation_type: Optional[str] = None
    timecode: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True
        