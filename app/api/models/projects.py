from pydantic import BaseModel
from typing import List
from datetime import datetime

class ProjectBase(BaseModel):
    name: str
    description: str | None = None

class ProjectCreate(ProjectBase):
    pass

class ProjectUpdate(ProjectBase):
    pass

class ProjectCollaboratorCreate(BaseModel):
    user_id: int
    role: str

class ProjectCollaboratorUpdate(BaseModel):
    role: str

class CollaboratorEmailList(BaseModel):
    collaborator_emails: list[str]


class UserResponse(BaseModel):
    id: int
    name: str
    email: str
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True

class ProjectResponse(BaseModel):
    id: int
    name: str
    description: str
    created_at: datetime
    updated_at: datetime
    creator: UserResponse
    collaborators: List[UserResponse]

    class Config:
        orm_mode = True
