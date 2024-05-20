from pydantic import BaseModel

class ActivityFeedBase(BaseModel):
    project_id: int
    user_id: int
    action: str
    meta_info: Optional[str] = None

class ActivityFeedCreate(ActivityFeedBase):
    pass

class ActivityFeedUpdate(ActivityFeedBase):
    pass

class ActivityFeedInDBBase(ActivityFeedBase):
    id: int
    created_at: str

    class Config:
        orm_mode = True

class ActivityFeed(ActivityFeedInDBBase):
    pass

class ActivityFeedInDB(ActivityFeedInDBBase):
    pass
