from sqlalchemy import Column, Integer, String, ForeignKey, Text, Boolean, ARRAY
from sqlalchemy.dialects.postgresql import JSONB, NUMRANGE
from sqlalchemy.orm import relationship
from sqlalchemy.sql.sqltypes import TIMESTAMP
from sqlalchemy.sql import func

from .database import Base

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    name = Column(String)
    role = Column(String)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    # Define the relationship to projects
    projects = relationship("Project", back_populates="creator")

class Project(Base):
    __tablename__ = "projects"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)
    description = Column(Text)
    creator_id = Column(Integer, ForeignKey("users.id"))
    creator = relationship("User", back_populates="projects")
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    collaborators = relationship("ProjectCollaborator", back_populates="project")
    videos = relationship("Video", back_populates="project")

class ProjectCollaborator(Base):
    __tablename__ = "project_collaborators"
    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"))
    user_id = Column(Integer, ForeignKey("users.id"))
    role = Column(String)
    project = relationship("Project", back_populates="collaborators")
    user = relationship("User")
    created_at = Column(TIMESTAMP, server_default=func.now())

class Video(Base):
    __tablename__ = "videos"
    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"))
    name = Column(String)
    description = Column(Text)
    version = Column(Integer)
    file_path = Column(String)
    uploader_id = Column(Integer, ForeignKey("users.id"))
    uploader = relationship("User")
    project = relationship("Project", back_populates="videos")
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    comments = relationship("Comment", back_populates="video")
    annotations = relationship("Annotation", back_populates="video")

class Comment(Base):
    __tablename__ = "comments"
    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(Integer, ForeignKey("videos.id"))
    user_id = Column(Integer, ForeignKey("users.id"))
    text = Column(Text)
    timecode = Column(Integer)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    video = relationship("Video", back_populates="comments")
    user = relationship("User")


class Annotation(Base):
    __tablename__ = "annotations"
    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(Integer, ForeignKey("videos.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    annotation_type = Column(String(50), nullable=False)
    annotation_data = Column(JSONB, nullable=False)
    timecode = Column(String, nullable=False)  # Assuming this should be a string
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    video = relationship("Video", back_populates="annotations")
    user = relationship("User")

class Notification(Base):
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    type = Column(String)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True)
    video_id = Column(Integer, ForeignKey("videos.id"), nullable=True)
    comment_id = Column(Integer, ForeignKey("comments.id"), nullable=True)
    read = Column(Boolean, default=False)
    created_at = Column(TIMESTAMP, server_default=func.now())

    user = relationship("User")
    project = relationship("Project")
    video = relationship("Video")
    comment = relationship("Comment")

class ActivityFeed(Base):
    __tablename__ = "activity_feed"
    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"))
    user_id = Column(Integer, ForeignKey("users.id"))
    action = Column(String)
    meta_info = Column(Text)
    created_at = Column(TIMESTAMP, server_default=func.now())

    project = relationship("Project")
    user = relationship("User")




# class ProjectAnalytics(Base):
#     __tablename__ = "ProjectAnalytics"

#     id = Column(Integer, primary_key=True, index=True)
#     projectId = Column(Integer, ForeignKey("projects.id"), nullable=False)
#     videoCount = Column(Integer, nullable=False, default=0)
#     commentCount = Column(Integer, nullable=False, default=0)
#     collaboratorCount = Column(Integer, nullable=False, default=0)
#     lastActivity = Column(TIMESTAMP)
#     createdAt = Column(TIMESTAMP, nullable=False, server_default=func.now())
#     updatedAt = Column(TIMESTAMP, nullable=False, server_default=func.now())

#     project = relationship("Project", back_populates="analytics")

# class UserAnalytics(Base):
#     __tablename__ = "UserAnalytics"

#     id = Column(Integer, primary_key=True, index=True)
#     userId = Column(Integer, ForeignKey("users.id"), nullable=False)
#     projectsCollaborated = Column(ARRAY(Integer))
#     videosUploaded = Column(Integer, nullable=False, default=0)
#     commentsPosted = Column(Integer, nullable=False, default=0)
#     lastActivity = Column(TIMESTAMP)
#     createdAt = Column(TIMESTAMP, nullable=False, server_default=func.now())
#     updatedAt = Column(TIMESTAMP, nullable=False, server_default=func.now())

#     user = relationship("User", back_populates="analytics")