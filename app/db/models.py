from sqlalchemy import Column, Integer, String, ForeignKey, Text, Boolean, ARRAY, UniqueConstraint
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
    stripe_connect_account_id = Column(String, unique=True, index=True, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    # Define the relationship to projects
    projects = relationship("Project", back_populates="creator")
    subscriptions = relationship(
        "Subscription", back_populates="user", cascade="all, delete-orphan"
    )
    settings = relationship(
        "UserSettings",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )
    youtube_connection = relationship(
        "UserYoutubeConnection",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )
    workspace_memberships = relationship(
        "WorkspaceMember",
        back_populates="user",
        cascade="all, delete-orphan",
    )


class UserSettings(Base):
    __tablename__ = "user_settings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    workspace_name = Column(String, server_default="My Workspace", nullable=False)
    timezone = Column(String, server_default="America/Los_Angeles", nullable=False)
    theme = Column(String, server_default="system", nullable=False)
    date_format = Column(String, server_default="MMM d, yyyy", nullable=False)
    email_comments = Column(Boolean, server_default="true", nullable=False)
    email_mentions = Column(Boolean, server_default="true", nullable=False)
    # off | daily | weekly — digest of @mentions (immediate emails still follow email_mentions)
    email_mention_digest = Column(String, server_default="off", nullable=False)
    product_updates = Column(Boolean, server_default="false", nullable=False)
    two_factor = Column(Boolean, server_default="false", nullable=False)
    session_timeout = Column(String, server_default="30", nullable=False)
    allow_project_invites = Column(Boolean, server_default="true", nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User", back_populates="settings")


class UserSession(Base):
    __tablename__ = "user_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    session_id = Column(String, unique=True, nullable=False, index=True)
    last_activity_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    revoked = Column(Boolean, server_default="false", nullable=False)
    revoked_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User")


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


class Workspace(Base):
    __tablename__ = "workspaces"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    slug = Column(String, unique=True, index=True, nullable=False)
    owner_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    settings = Column(JSONB, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    owner = relationship("User", foreign_keys=[owner_user_id])
    members = relationship(
        "WorkspaceMember",
        back_populates="workspace",
        cascade="all, delete-orphan",
    )
    projects = relationship("Project", back_populates="workspace")
    branding = relationship(
        "WorkspaceBranding",
        back_populates="workspace",
        uselist=False,
        cascade="all, delete-orphan",
    )
    invites = relationship(
        "WorkspaceInvite",
        back_populates="workspace",
        cascade="all, delete-orphan",
    )
    assets = relationship(
        "WorkspaceAsset",
        back_populates="workspace",
        cascade="all, delete-orphan",
    )


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(
        Integer,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # owner | producer | editor | assistant | client | guest
    role = Column(String, nullable=False, server_default="editor")
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)

    workspace = relationship("Workspace", back_populates="members")
    user = relationship("User", back_populates="workspace_memberships")

    __table_args__ = (UniqueConstraint("workspace_id", "user_id", name="uq_workspace_members_ws_user"),)


class WorkspaceInvite(Base):
    __tablename__ = "workspace_invites"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    email = Column(String, nullable=False, index=True)
    role = Column(String, nullable=False, server_default="editor")
    token = Column(String, unique=True, index=True, nullable=False)
    invited_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    expires_at = Column(TIMESTAMP, nullable=False)
    accepted_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)

    workspace = relationship("Workspace", back_populates="invites")


class WorkspaceBranding(Base):
    __tablename__ = "workspace_brandings"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(
        Integer,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    logo_url = Column(String, nullable=True)
    primary_color = Column(String, nullable=True)
    accent_color = Column(String, nullable=True)
    client_footer_text = Column(Text, nullable=True)
    custom_domain = Column(String, unique=True, index=True, nullable=True)
    domain_verification_token = Column(String, nullable=True)
    domain_verified_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    workspace = relationship("Workspace", back_populates="branding")


class ProjectTemplate(Base):
    """Preset folder trees + review stages (system or per-workspace)."""

    __tablename__ = "project_templates"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True, index=True)
    template_key = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)
    definition = Column(JSONB, nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)

    workspace = relationship("Workspace", foreign_keys=[workspace_id])


class WorkspaceAsset(Base):
    __tablename__ = "workspace_assets"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    category = Column(String, nullable=False, index=True)
    title = Column(String, nullable=False)
    file_url = Column(String, nullable=False)
    extra = Column(JSONB, nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)

    workspace = relationship("Workspace", back_populates="assets")
    project_links = relationship(
        "ProjectWorkspaceAssetLink",
        back_populates="workspace_asset",
        cascade="all, delete-orphan",
    )


class ProjectWorkspaceAssetLink(Base):
    """Attach a shared workspace library asset to a project (reference row)."""

    __tablename__ = "project_workspace_asset_links"
    __table_args__ = (
        UniqueConstraint("project_id", "workspace_asset_id", name="uq_project_workspace_asset"),
    )

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    workspace_asset_id = Column(
        Integer,
        ForeignKey("workspace_assets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    folder_id = Column(Integer, ForeignKey("folders.id", ondelete="SET NULL"), nullable=True, index=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)

    project = relationship("Project", back_populates="workspace_asset_links")
    workspace_asset = relationship("WorkspaceAsset", back_populates="project_links")
    folder = relationship("Folder", back_populates="workspace_asset_links")
    created_by = relationship("User", foreign_keys=[created_by_user_id])


class Project(Base):
    __tablename__ = "projects"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)
    description = Column(Text)
    creator_id = Column(Integer, ForeignKey("users.id"))
    workspace_id = Column(
        Integer,
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    created_from_template_id = Column(
        Integer,
        ForeignKey("project_templates.id", ondelete="SET NULL"),
        nullable=True,
    )
    creator = relationship("User", back_populates="projects")
    workspace = relationship("Workspace", back_populates="projects")
    created_from_template = relationship("ProjectTemplate", foreign_keys=[created_from_template_id])
    # Freelancer business layer
    scope_revisions_included = Column(Integer, server_default="3", nullable=False)
    revision_count = Column(Integer, server_default="0", nullable=False)
    change_request_fee_cents = Column(Integer, server_default="0", nullable=False)
    currency = Column(String, server_default="USD", nullable=False)
    hourly_rate_cents = Column(Integer, nullable=True)
    deliverables_locked = Column(Boolean, server_default="true", nullable=False)
    portfolio_public = Column(Boolean, server_default="false", nullable=False)
    portfolio_slug = Column(String, unique=True, index=True, nullable=True)
    client_name = Column(String, nullable=True)
    client_email = Column(String, nullable=True)
    rate_card_cents = Column(JSONB, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    collaborators = relationship("ProjectCollaborator", back_populates="project")
    videos = relationship("Video", back_populates="project")
    folders = relationship("Folder", back_populates="project", cascade="all, delete-orphan")
    review_workflow_templates = relationship(
        "ReviewWorkflowTemplate",
        back_populates="project",
        cascade="all, delete-orphan",
    )
    workspace_asset_links = relationship(
        "ProjectWorkspaceAssetLink",
        back_populates="project",
        cascade="all, delete-orphan",
    )


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
    workspace_asset_links = relationship(
        "ProjectWorkspaceAssetLink",
        back_populates="folder",
    )
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
    ai_results = relationship(
        "AiResult",
        back_populates="video",
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
    speakers = Column(JSONB, nullable=True)
    speaker_count = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    model_name = Column(String, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    video = relationship("Video", back_populates="transcription")


class AiResult(Base):
    __tablename__ = "ai_results"

    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(
        Integer,
        ForeignKey("videos.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    result_type = Column(String, nullable=False, index=True)
    status = Column(
        String,
        nullable=False,
        server_default="completed",
    )  # pending, processing, completed, failed
    result_data = Column(JSONB, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    video = relationship("Video", back_populates="ai_results")


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
    # public (client-visible on review) | team (internal) | author_only (legacy private note)
    visibility = Column(String, server_default="public", nullable=False, index=True)
    due_at = Column(TIMESTAMP, nullable=True)
    # comment | change_request — change requests can block client approval until resolved/wontfix
    kind = Column(String, server_default="comment", nullable=False, index=True)
    # open | in_progress | resolved | wontfix | reopened
    status = Column(String, server_default="open", nullable=False, index=True)
    assignee_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    status_changed_at = Column(TIMESTAMP, nullable=True)
    # Guest (review-link) commenter fields — used when user_id is null
    guest_name = Column(String, nullable=True)
    guest_email = Column(String, nullable=True)
    guest_avatar_url = Column(String, nullable=True)
    review_link_id = Column(
        Integer, ForeignKey("review_links.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    video = relationship("Video", back_populates="comments")
    user = relationship("User", foreign_keys=[user_id])
    assignee = relationship("User", foreign_keys=[assignee_user_id])
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
    approval_required_for_download = Column(
        Boolean, server_default="false", nullable=False
    )
    allow_comments = Column(Boolean, server_default="true", nullable=False)
    watermark_enabled = Column(Boolean, server_default="true", nullable=False)
    require_email = Column(Boolean, server_default="false", nullable=False)
    version_group_id = Column(String, nullable=True, index=True)
    version_label = Column(String, nullable=True)
    revoked_at = Column(TIMESTAMP, nullable=True)
    allow_export = Column(Boolean, server_default="false", nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(
        TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    video = relationship("Video")
    creator = relationship("User")
    sessions = relationship(
        "ReviewSession", back_populates="review_link", cascade="all, delete-orphan"
    )
    workflow_run = relationship(
        "ReviewWorkflowRun",
        back_populates="review_link",
        uselist=False,
        cascade="all, delete-orphan",
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
    guest_avatar_url = Column(String, nullable=True)
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
    signoffs = relationship(
        "ReviewSignoff", back_populates="session", cascade="all, delete-orphan"
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


class ReviewMagicToken(Base):
    __tablename__ = "review_magic_tokens"

    id = Column(Integer, primary_key=True, index=True)
    review_link_id = Column(
        Integer,
        ForeignKey("review_links.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    email = Column(String, nullable=False, index=True)
    guest_name = Column(String, nullable=True)
    token_hash = Column(String, nullable=False, unique=True, index=True)
    fingerprint = Column(String, nullable=True, index=True)
    ip_address = Column(String, nullable=True)
    invited_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    source = Column(String, nullable=False, server_default="self_service")
    expires_at = Column(TIMESTAMP, nullable=False)
    used_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)

    review_link = relationship("ReviewLink")
    inviter = relationship("User")


class ReviewSignoff(Base):
    __tablename__ = "review_signoffs"

    id = Column(Integer, primary_key=True, index=True)
    review_link_id = Column(
        Integer,
        ForeignKey("review_links.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id = Column(
        Integer,
        ForeignKey("review_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    signer_name = Column(String, nullable=True)
    signer_email = Column(String, nullable=True)
    declaration_text = Column(Text, nullable=False)
    legal_snapshot_json = Column(JSONB, nullable=True)
    signed_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    signature_type = Column(String, server_default="none", nullable=False)  # none | typed | drawn
    typed_signature = Column(Text, nullable=True)
    signature_image_data = Column(Text, nullable=True)  # data URL or raw base64
    pdf_url = Column(String, nullable=True)

    review_link = relationship("ReviewLink")
    session = relationship("ReviewSession", back_populates="signoffs")


class ReviewWorkflowTemplate(Base):
    """Ordered approval stages for a project (Editor → Producer → …)."""

    __tablename__ = "review_workflow_templates"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String, nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    project = relationship("Project", back_populates="review_workflow_templates")
    stages = relationship(
        "ReviewWorkflowStage",
        back_populates="template",
        cascade="all, delete-orphan",
    )
    runs = relationship("ReviewWorkflowRun", back_populates="template")


class ReviewWorkflowStage(Base):
    __tablename__ = "review_workflow_stages"

    id = Column(Integer, primary_key=True, index=True)
    template_id = Column(
        Integer,
        ForeignKey("review_workflow_templates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    stage_index = Column(Integer, nullable=False)
    stage_key = Column(String, nullable=False)
    label = Column(String, nullable=False)
    notify_user_ids = Column(JSONB, nullable=False, server_default="[]")

    template = relationship("ReviewWorkflowTemplate", back_populates="stages")


class ReviewWorkflowRun(Base):
    """Runtime progress of a workflow for one review link."""

    __tablename__ = "review_workflow_runs"

    id = Column(Integer, primary_key=True, index=True)
    review_link_id = Column(
        Integer,
        ForeignKey("review_links.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    template_id = Column(
        Integer,
        ForeignKey("review_workflow_templates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    current_stage_index = Column(Integer, server_default="0", nullable=False)
    completed_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    review_link = relationship("ReviewLink", back_populates="workflow_run")
    template = relationship("ReviewWorkflowTemplate", back_populates="runs")


class ReviewCommentDraft(Base):
    __tablename__ = "review_comment_drafts"

    id = Column(Integer, primary_key=True, index=True)
    review_link_id = Column(
        Integer,
        ForeignKey("review_links.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id = Column(
        Integer,
        ForeignKey("review_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    video_id = Column(
        Integer,
        ForeignKey("videos.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    text = Column(Text, nullable=False)
    timecode = Column(Integer, nullable=False, server_default="0")
    updated_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)


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


class Suggestion(Base):
    __tablename__ = "suggestions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String, nullable=False)
    body = Column(Text, nullable=False)
    category = Column(String, nullable=True)
    status = Column(String, server_default="open", nullable=False)  # open|planned|in_progress|completed
    upvotes_count = Column(Integer, server_default="0", nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User")
    comments = relationship("SuggestionComment", back_populates="suggestion", cascade="all, delete-orphan")
    votes = relationship("SuggestionVote", back_populates="suggestion", cascade="all, delete-orphan")


class SuggestionComment(Base):
    __tablename__ = "suggestion_comments"

    id = Column(Integer, primary_key=True, index=True)
    suggestion_id = Column(
        Integer, ForeignKey("suggestions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    body = Column(Text, nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    suggestion = relationship("Suggestion", back_populates="comments")
    user = relationship("User")


class SuggestionVote(Base):
    __tablename__ = "suggestion_votes"
    __table_args__ = (
        UniqueConstraint("suggestion_id", "user_id", name="uq_suggestion_votes_suggestion_user"),
    )

    id = Column(Integer, primary_key=True, index=True)
    suggestion_id = Column(
        Integer, ForeignKey("suggestions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)

    suggestion = relationship("Suggestion", back_populates="votes")
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


# =====================================================================
# Creator-native models (YouTube publish, social exports, thumbnails)
# =====================================================================


class VideoPublication(Base):
    __tablename__ = "video_publications"

    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(Integer, ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    platform = Column(String, nullable=False)  # youtube|tiktok|instagram|twitter
    status = Column(String, server_default="draft", nullable=False)
    title = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    tags = Column(Text, nullable=True)
    category = Column(String, nullable=True)
    privacy = Column(String, server_default="private", nullable=False)
    scheduled_at = Column(TIMESTAMP, nullable=True)
    published_at = Column(TIMESTAMP, nullable=True)
    external_id = Column(String, nullable=True)
    external_url = Column(String, nullable=True)
    thumbnail_variant_id = Column(Integer, nullable=True)
    extra = Column(JSONB, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    video = relationship("Video")
    creator = relationship("User")


class VideoAspectExport(Base):
    __tablename__ = "video_aspect_exports"

    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(Integer, ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True)
    aspect_ratio = Column(String, nullable=False)
    platform_preset = Column(String, nullable=True)
    status = Column(String, server_default="pending", nullable=False)
    output_path = Column(String, nullable=True)
    thumbnail_url = Column(String, nullable=True)
    duration = Column(Integer, nullable=True)
    subject_tracking = Column(Boolean, server_default="true", nullable=False)
    error_message = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    video = relationship("Video")


class VideoChapter(Base):
    __tablename__ = "video_chapters"

    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(Integer, ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True)
    start_time = Column(Integer, nullable=False)
    end_time = Column(Integer, nullable=True)
    title = Column(String, nullable=False)
    source = Column(String, server_default="manual", nullable=False)
    order_index = Column(Integer, server_default="0", nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)

    video = relationship("Video")


class VideoEndScreen(Base):
    __tablename__ = "video_end_screens"

    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(Integer, ForeignKey("videos.id", ondelete="CASCADE"), unique=True, nullable=False)
    cards = Column(JSONB, nullable=True)
    pinned_comment = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    video = relationship("Video")


class BrandDeal(Base):
    __tablename__ = "brand_deals"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    video_id = Column(Integer, ForeignKey("videos.id", ondelete="SET NULL"), nullable=True)
    sponsor_name = Column(String, nullable=False)
    contact_email = Column(String, nullable=True)
    amount_cents = Column(Integer, server_default="0", nullable=False)
    currency = Column(String, server_default="USD", nullable=False)
    segment_start = Column(Integer, nullable=True)
    segment_end = Column(Integer, nullable=True)
    integration_notes = Column(Text, nullable=True)
    payout_status = Column(String, server_default="pending", nullable=False)
    paid_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    project = relationship("Project")
    video = relationship("Video")


class ThumbnailVariant(Base):
    __tablename__ = "thumbnail_variants"

    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(Integer, ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True)
    label = Column(String, nullable=True)
    image_url = Column(String, nullable=False)
    is_winner = Column(Boolean, server_default="false", nullable=False)
    impressions = Column(Integer, server_default="0", nullable=False)
    clicks = Column(Integer, server_default="0", nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)

    video = relationship("Video")


class UserYoutubeConnection(Base):
    """YouTube Data API OAuth tokens (separate from Google login). One row per user."""

    __tablename__ = "user_youtube_connections"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False, index=True)
    channel_id = Column(String, nullable=True)
    channel_title = Column(String, nullable=True)
    refresh_token_encrypted = Column(Text, nullable=False)
    access_token = Column(Text, nullable=True)
    access_expires_at = Column(TIMESTAMP, nullable=True)
    scopes = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User", back_populates="youtube_connection")


# =====================================================================
# Freelancer Business Layer models
# =====================================================================


class ProjectRevision(Base):
    __tablename__ = "project_revisions"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    video_id = Column(Integer, ForeignKey("videos.id", ondelete="SET NULL"), nullable=True)
    round_number = Column(Integer, nullable=False)
    triggered_by = Column(String, nullable=True)
    note = Column(Text, nullable=True)
    billable = Column(Boolean, server_default="false", nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)

    project = relationship("Project")
    video = relationship("Video")


class Invoice(Base):
    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    number = Column(String, nullable=True)
    client_name = Column(String, nullable=True)
    client_email = Column(String, nullable=True)
    currency = Column(String, server_default="USD", nullable=False)
    subtotal_cents = Column(Integer, server_default="0", nullable=False)
    tax_cents = Column(Integer, server_default="0", nullable=False)
    total_cents = Column(Integer, server_default="0", nullable=False)
    status = Column(String, server_default="draft", nullable=False)
    stripe_invoice_id = Column(String, nullable=True)
    stripe_payment_link = Column(String, nullable=True)
    stripe_connect_account_id = Column(String, nullable=True, index=True)
    due_at = Column(TIMESTAMP, nullable=True)
    sent_at = Column(TIMESTAMP, nullable=True)
    paid_at = Column(TIMESTAMP, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    project = relationship("Project")
    items = relationship("InvoiceItem", back_populates="invoice", cascade="all, delete-orphan")


class InvoiceItem(Base):
    __tablename__ = "invoice_items"

    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True)
    description = Column(Text, nullable=False)
    quantity = Column(Integer, server_default="1", nullable=False)
    unit_price_cents = Column(Integer, server_default="0", nullable=False)
    total_cents = Column(Integer, server_default="0", nullable=False)

    invoice = relationship("Invoice", back_populates="items")


class ProjectMilestone(Base):
    __tablename__ = "project_milestones"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String, nullable=False)
    amount_cents = Column(Integer, server_default="0", nullable=False)
    currency = Column(String, server_default="USD", nullable=False)
    percentage = Column(Integer, nullable=True)
    due_at = Column(TIMESTAMP, nullable=True)
    status = Column(String, server_default="pending", nullable=False)
    invoice_id = Column(Integer, ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True)
    order_index = Column(Integer, server_default="0", nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    project = relationship("Project")


class Contract(Base):
    __tablename__ = "contracts"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String, nullable=False)
    body = Column(Text, nullable=False)
    status = Column(String, server_default="draft", nullable=False)
    signer_name = Column(String, nullable=True)
    signer_email = Column(String, nullable=True)
    signature_data = Column(Text, nullable=True)
    signed_at = Column(TIMESTAMP, nullable=True)
    signing_token = Column(String, unique=True, index=True, nullable=True)
    pdf_url = Column(String, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    project = relationship("Project")


class TimeEntry(Base):
    __tablename__ = "time_entries"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    started_at = Column(TIMESTAMP, nullable=False)
    ended_at = Column(TIMESTAMP, nullable=True)
    duration_seconds = Column(Integer, server_default="0", nullable=False)
    note = Column(Text, nullable=True)
    billable = Column(Boolean, server_default="true", nullable=False)
    hourly_rate_cents = Column(Integer, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)

    project = relationship("Project")
    user = relationship("User")


class ProjectEstimate(Base):
    __tablename__ = "project_estimates"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String, nullable=True)
    runtime_minutes = Column(Integer, server_default="0", nullable=False)
    complexity = Column(String, server_default="standard", nullable=False)
    rate_cents_per_hour = Column(Integer, server_default="0", nullable=False)
    estimated_hours = Column(Integer, server_default="0", nullable=False)
    line_items = Column(JSONB, nullable=True)
    total_cents = Column(Integer, server_default="0", nullable=False)
    currency = Column(String, server_default="USD", nullable=False)
    status = Column(String, server_default="draft", nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    project = relationship("Project")




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