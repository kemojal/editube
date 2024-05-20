from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class ProjectAnalyticsBase(BaseModel):
    projectId: int
    videoCount: int = 0
    commentCount: int = 0
    collaboratorCount: int = 0
    lastActivity: Optional[datetime] = None

class ProjectAnalyticsCreate(ProjectAnalyticsBase):
    pass

class ProjectAnalyticsUpdate(ProjectAnalyticsBase):
    pass

class ProjectAnalytics(ProjectAnalyticsBase):
    id: int
    createdAt: datetime
    updatedAt: datetime

    class Config:
        orm_mode = True

class UserAnalyticsBase(BaseModel):
    userId: int
    projectsCollaborated: list[int] = []
    videosUploaded: int = 0
    commentsPosted: int = 0
    lastActivity: Optional[datetime] = None

class UserAnalyticsCreate(UserAnalyticsBase):
    pass

class UserAnalyticsUpdate(UserAnalyticsBase):
    pass

class UserAnalytics(UserAnalyticsBase):
    id: int
    createdAt: datetime
    updatedAt: datetime

    class Config:
        orm_mode = True