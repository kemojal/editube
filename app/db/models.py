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
    hashed_password = Column(String, nullable=True)
    name = Column(String)
    full_name = Column(String, nullable=True)
    role = Column(String)
    avatar_url = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    workflow_type = Column(String, nullable=True)  # "agency", "freelancer", "internal"
    plan = Column(String, nullable=True)  # "basic", "pro", "elite"
    onboarding_completed = Column(Boolean, server_default="false", nullable=False)
    trial_start_date = Column(TIMESTAMP, nullable=True)
    stripe_customer_id = Column(String, nullable=True)
    stripe_subscription_id = Column(String, nullable=True)
    subscription_status = Column(String, nullable=True)
    auth_provider = Column(String, nullable=True, server_default="local")
    google_sub = Column(String, unique=True, index=True, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    # Define the relationship to projects
    projects = relationship("Project", back_populates="creator")
    subscriptions = relationship(
        "Subscription", back_populates="user", cascade="all, delete-orphan"
    )


class Subscription(Base):
    """One row per Stripe subscription id (history preserved when customers resubscribe)."""

    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    stripe_subscription_id = Column(String, unique=True, index=True, nullable=False)
    stripe_customer_id = Column(String, nullable=True)
    stripe_price_id = Column(String, nullable=True)
    customer_email = Column(String, nullable=True)  # snapshot from User at sync time
    status = Column(String, nullable=False)
    plan = Column(String, nullable=True)  # basic | pro | elite (from metadata)
    trial_start = Column(TIMESTAMP, nullable=True)
    current_period_start = Column(TIMESTAMP, nullable=True)
    current_period_end = Column(TIMESTAMP, nullable=True)
    cancel_at_period_end = Column(Boolean, server_default="false", nullable=False)
    ended_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="subscriptions")


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
    folders = relationship("Folder", back_populates="project", cascade="all, delete-orphan")

class ProjectCollaborator(Base):
    __tablename__ = "project_collaborators"
    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"))
    user_id = Column(Integer, ForeignKey("users.id"))
    role = Column(String)
    project = relationship("Project", back_populates="collaborators")
    user = relationship("User")
    created_at = Column(TIMESTAMP, server_default=func.now())

class Folder(Base):
    __tablename__ = "folders"
    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    parent_id = Column(Integer, ForeignKey("folders.id"), nullable=True)
    name = Column(String, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    project = relationship("Project", back_populates="folders")
    parent = relationship("Folder", remote_side=[id], back_populates="children")
    children = relationship("Folder", back_populates="parent", cascade="all, delete-orphan")
    videos = relationship("Video", back_populates="folder", cascade="all, delete-orphan")
    creator = relationship("User")

class Video(Base):
    __tablename__ = "videos"
    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"))
    folder_id = Column(Integer, ForeignKey("folders.id"), nullable=True)
    name = Column(String)
    description = Column(Text)
    version = Column(Integer)
    file_path = Column(String)
    thumbnail_url = Column(String, nullable=True)
    status = Column(String, server_default="in_progress", nullable=False)  # in_progress, in_review, approved, needs_changes
    duration = Column(Integer, nullable=True)  # duration in seconds
    uploader_id = Column(Integer, ForeignKey("users.id"))
    uploader = relationship("User")
    project = relationship("Project", back_populates="videos")
    folder = relationship("Folder", back_populates="videos")
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    comments = relationship("Comment", back_populates="video")
    annotations = relationship("Annotation", back_populates="video")
    transcription = relationship(
        "VideoTranscription",
        back_populates="video",
        uselist=False,
        cascade="all, delete-orphan",
    )


class VideoTranscription(Base):
    __tablename__ = "video_transcriptions"

    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(
        Integer,
        ForeignKey("videos.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    status = Column(
        String,
        nullable=False,
        server_default="pending",
    )  # pending, queued, processing, completed, failed
    segments = Column(JSONB, nullable=True)
    error_message = Column(Text, nullable=True)
    model_name = Column(String, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    video = relationship("Video", back_populates="transcription")


class Comment(Base):
    __tablename__ = "comments"
    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(Integer, ForeignKey("videos.id"))
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    parent_id = Column(Integer, ForeignKey("comments.id"), nullable=True)
    text = Column(Text)
    timecode = Column(Integer)
    end_timecode = Column(Integer, nullable=True)  # null means point comment, set means range comment
    drawing_data = Column(JSONB, nullable=True)  # FabricJS canvas objects drawn with the comment
    is_resolved = Column(Boolean, server_default="false", nullable=False)
    is_private = Column(Boolean, server_default="false", nullable=False)
    # Guest (review-link) commenter fields — used when user_id is null
    guest_name = Column(String, nullable=True)
    guest_email = Column(String, nullable=True)
    review_link_id = Column(
        Integer, ForeignKey("review_links.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    video = relationship("Video", back_populates="comments")
    user = relationship("User")
    parent = relationship("Comment", remote_side=[id], back_populates="replies")
    replies = relationship("Comment", back_populates="parent", cascade="all, delete-orphan")
    likes = relationship("CommentLike", back_populates="comment", cascade="all, delete-orphan")
    review_link = relationship("ReviewLink")


class ReviewLink(Base):
    """Tokenised no-signup review link for a video."""

    __tablename__ = "review_links"

    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(
        Integer, ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    token = Column(String, unique=True, index=True, nullable=False)
    # Human label shown to creator in UI ("Client Q1 review")
    label = Column(String, nullable=True)
    password_hash = Column(String, nullable=True)
    expires_at = Column(TIMESTAMP, nullable=True)
    allow_download = Column(Boolean, server_default="false", nullable=False)
    allow_comments = Column(Boolean, server_default="true", nullable=False)
    watermark_enabled = Column(Boolean, server_default="true", nullable=False)
    require_email = Column(Boolean, server_default="false", nullable=False)
    revoked_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(
        TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    video = relationship("Video")
    creator = relationship("User")
    sessions = relationship(
        "ReviewSession", back_populates="review_link", cascade="all, delete-orphan"
    )


class ReviewSession(Base):
    """One row per guest (identified by fingerprint cookie) on a review link."""

    __tablename__ = "review_sessions"

    id = Column(Integer, primary_key=True, index=True)
    review_link_id = Column(
        Integer,
        ForeignKey("review_links.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    fingerprint = Column(String, nullable=False, index=True)
    guest_name = Column(String, nullable=True)
    guest_email = Column(String, nullable=True)
    ip_address = Column(String, nullable=True)
    user_agent = Column(Text, nullable=True)
    total_watch_seconds = Column(Integer, server_default="0", nullable=False)
    max_position = Column(Integer, server_default="0", nullable=False)  # furthest point reached, seconds
    reached_end = Column(Boolean, server_default="false", nullable=False)
    view_count = Column(Integer, server_default="0", nullable=False)
    first_viewed_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    last_viewed_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    approved_at = Column(TIMESTAMP, nullable=True)

    review_link = relationship("ReviewLink", back_populates="sessions")
    events = relationship(
        "ReviewEvent", back_populates="session", cascade="all, delete-orphan"
    )


class ReviewEvent(Base):
    """Granular watch events for heatmap / analytics."""

    __tablename__ = "review_events"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(
        Integer,
        ForeignKey("review_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type = Column(String, nullable=False)  # play|pause|seek|progress|ended|comment
    position = Column(Integer, nullable=False)  # seconds
    # For progress events: range end (exclusive); lets us build heatmaps cheaply
    range_end = Column(Integer, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)

    session = relationship("ReviewSession", back_populates="events")


class Annotation(Base):
    __tablename__ = "annotations"
    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(Integer, ForeignKey("videos.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    annotation_type = Column(String(50), nullable=False)
    annotation_data = Column(JSONB, nullable=False)
    timecode = Column(Integer, nullable=False)
    duration = Column(Integer, server_default="5", nullable=False)
    is_private = Column(Boolean, server_default="false", nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    video = relationship("Video", back_populates="annotations")
    user = relationship("User")

class CommentLike(Base):
    __tablename__ = "comment_likes"
    id = Column(Integer, primary_key=True, index=True)
    comment_id = Column(Integer, ForeignKey("comments.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now())

    comment = relationship("Comment", back_populates="likes")
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