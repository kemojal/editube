# from pydantic import BaseModel, EmailStr

# class UserBase(BaseModel):
#     email: EmailStr
#     name: str
#     role: str

# class UserCreate(UserBase):
#     password: str

# class UserUpdate(UserBase):
#     password: str | None = None

# class UserLogin(BaseModel):
#     email: EmailStr
#     password: str

# app/api/models/users.py

from pydantic import BaseModel, EmailStr, ConfigDict
from typing import Optional
from typing import Literal
from sqlalchemy import Column, Integer, String
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime
class UserBase(BaseModel):
    email: EmailStr
    name: str
    role: str


class UserResponse(BaseModel):
    id: int
    name: str
    email: str
    role: str
    full_name: Optional[str] = None
    avatar_url: Optional[str] = None
    phone: Optional[str] = None
    workflow_type: Optional[str] = None
    plan: Optional[str] = None
    onboarding_completed: bool = False
    subscription_status: Optional[str] = None
    stripe_customer_id: Optional[str] = None
    auth_provider: Optional[str] = None
    google_sub: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class UserSettingsResponse(BaseModel):
    workspace_name: str = "My Workspace"
    timezone: str = "America/Los_Angeles"
    theme: str = "system"
    date_format: str = "MMM d, yyyy"
    email_comments: bool = True
    email_mentions: bool = True
    email_mention_digest: str = "off"
    product_updates: bool = False
    two_factor: bool = False
    session_timeout: str = "30"
    allow_project_invites: bool = True

    model_config = ConfigDict(from_attributes=True)


class UserSettingsUpdate(BaseModel):
    workspace_name: Optional[str] = None
    timezone: Optional[str] = None
    theme: Optional[Literal["light", "dark", "system"]] = None
    date_format: Optional[Literal["MMM d, yyyy", "yyyy-MM-dd", "MM/dd/yyyy"]] = None
    email_comments: Optional[bool] = None
    email_mentions: Optional[bool] = None
    email_mention_digest: Optional[Literal["off", "daily", "weekly"]] = None
    product_updates: Optional[bool] = None
    two_factor: Optional[bool] = None
    session_timeout: Optional[Literal["15", "30", "60", "120"]] = None
    allow_project_invites: Optional[bool] = None


class OnboardingProfileUpdate(BaseModel):
    full_name: str
    phone: Optional[str] = None
    avatar_url: Optional[str] = None


class OnboardingWorkflowUpdate(BaseModel):
    workflow_type: str  # "agency", "freelancer", "internal"


class OnboardingPlanUpdate(BaseModel):
    plan: str  # "basic", "pro", "elite"

class UserCreate(UserBase):
    password: str

class UserUpdate(UserBase):
    password: str | None = None

class UserLogin(BaseModel):
    email: EmailStr
    password: str

# Example SQLAlchemy model for Users

Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    name = Column(String)
    role = Column(String)
    hashed_password = Column(String)





class UserRegisterSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    email: str
    hashed_password: str
    name: str
    role: str


class UserLoginSchema(BaseModel):
    email: str
    password: str



class GoogleAccountBase(BaseModel):
    userId: int
    googleId: str
    accessToken: str
    refreshToken: str

class GoogleAccountCreate(GoogleAccountBase):
    pass

class GoogleAccountUpdate(GoogleAccountBase):
    pass

class GoogleAccount(GoogleAccountBase):
    id: int
    createdAt: datetime
    updatedAt: datetime

    model_config = ConfigDict(from_attributes=True)