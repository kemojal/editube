from pydantic import BaseModel
from typing import Optional

class NotificationBase(BaseModel):
    type: str
    project_id: Optional[int] = None
    video_id: Optional[int] = None
    comment_id: Optional[int] = None
    read: bool = False

class NotificationCreate(NotificationBase):
    pass

class NotificationUpdate(NotificationBase):
    read: bool

class NotificationInDBBase(NotificationBase):
    id: int
    user_id: int
    created_at: str

    model_config = {"from_attributes": True}

class Notification(NotificationInDBBase):
    pass

class NotificationInDB(NotificationInDBBase):
    pass
