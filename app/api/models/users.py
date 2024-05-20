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
from sqlalchemy import Column, Integer, String
from sqlalchemy.ext.declarative import declarative_base
class UserBase(BaseModel):
    email: EmailStr
    name: str
    role: str

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
