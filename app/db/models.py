from sqlalchemy import Column, Integer, Float, String, ForeignKey, Text, Boolean, ARRAY, UniqueConstraint
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
    plan = Column(String, nullable=True)  # "free", "pro", "scale", "enterprise"
    onboarding_completed = Column(Boolean, server_default="false", nullable=False)
    trial_start_date = Column(TIMESTAMP, nullable=True)
    storage_grace_until = Column(TIMESTAMP, nullable=True)
    stripe_customer_id = Column(String, nullable=True)
    stripe_subscription_id = Column(String, nullable=True)
    subscription_status = Column(String, nullable=True)
    auth_provider = Column(String, nullable=True, server_default="local")
    google_sub = Column(String, unique=True, index=True, nullable=True)
    stripe_connect_account_id = Column(String, unique=True, index=True, nullable=True)
    mfa_required = Column(Boolean, server_default="false", nullable=False)
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
    plan = Column(String, nullable=True)  # free | pro | scale | enterprise (from metadata)
    trial_start = Column(TIMESTAMP, nullable=True)
    current_period_start = Column(TIMESTAMP, nullable=True)
    current_period_end = Column(TIMESTAMP, nullable=True)
    cancel_at_period_end = Column(Boolean, server_default="false", nullable=False)
    ended_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="subscriptions")


class StripeProduct(Base):
    """Mirror of Stripe Product objects; kept in sync via webhooks or catalog sync."""

    __tablename__ = "stripe_products"

    id = Column(Integer, primary_key=True, index=True)
    stripe_product_id = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    active = Column(Boolean, server_default="true", nullable=False)
    metadata_json = Column("metadata", JSONB, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)


class StripePrice(Base):
    """Mirror of Stripe Price objects; checkout resolves stripe_price_id from editube_plan + interval."""

    __tablename__ = "stripe_prices"

    id = Column(Integer, primary_key=True, index=True)
    stripe_price_id = Column(String, unique=True, index=True, nullable=False)
    stripe_product_id = Column(
        String,
        ForeignKey("stripe_products.stripe_product_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    currency = Column(String, nullable=True)
    unit_amount = Column(Integer, nullable=True)
    nickname = Column(String, nullable=True)
    recurring_interval = Column(String, nullable=True)  # month | year | null (one_time)
    active = Column(Boolean, server_default="true", nullable=False)
    metadata_json = Column("metadata", JSONB, nullable=True)
    editube_plan = Column(String, nullable=True, index=True)
    editube_interval = Column(String, nullable=True, index=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)


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
    file_path = Column(Text, nullable=False)
    ingest_page_url = Column(Text, nullable=True)
    thumbnail_url = Column(String, nullable=True)
    status = Column(String, server_default="in_progress", nullable=False)  # in_progress, in_review, approved, needs_changes
    duration = Column(Integer, nullable=True)  # duration in seconds
    size_bytes = Column(Integer, server_default="0", nullable=False)
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
    proxies = relationship(
        "VideoProxy",
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
    transcript_segment_index = Column(Integer, nullable=True)
    word_start_index = Column(Integer, nullable=True)
    word_end_index = Column(Integer, nullable=True)
    anchor_text = Column(Text, nullable=True)
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
    client_mutation_id = Column(String, nullable=True, index=True)
    revision = Column(Integer, server_default="1", nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    video = relationship("Video", back_populates="comments")
    user = relationship("User", foreign_keys=[user_id])
    assignee = relationship("User", foreign_keys=[assignee_user_id])
    parent = relationship("Comment", remote_side=[id], back_populates="replies")
    replies = relationship("Comment", back_populates="parent", cascade="all, delete-orphan")
    likes = relationship("CommentLike", back_populates="comment", cascade="all, delete-orphan")
    review_link = relationship("ReviewLink")
    attachments = relationship(
        "CommentAttachment",
        back_populates="comment",
        cascade="all, delete-orphan",
    )


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
    watermark_mode = Column(String, server_default="visible_overlay", nullable=False)
    require_email = Column(Boolean, server_default="false", nullable=False)
    nda_required = Column(Boolean, server_default="false", nullable=False)
    nda_document_id = Column(Integer, ForeignKey("nda_documents.id", ondelete="SET NULL"), nullable=True)
    geofence_mode = Column(String, server_default="off", nullable=False)
    geo_allow_countries = Column(ARRAY(String), nullable=True)
    geo_block_countries = Column(ARRAY(String), nullable=True)
    recording_detection_mode = Column(String, server_default="monitor", nullable=False)
    version_group_id = Column(String, nullable=True, index=True)
    version_label = Column(String, nullable=True)
    revoked_at = Column(TIMESTAMP, nullable=True)
    revocation_reason = Column(String, nullable=True)
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
    country_code = Column(String, nullable=True)
    watermark_payload = Column(JSONB, nullable=True)

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
    seq = Column(Integer, nullable=True)
    meta_info = Column(JSONB, nullable=True)
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


class ReviewRoomMessage(Base):
    __tablename__ = "review_room_messages"

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
    body = Column(Text, nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)

    review_link = relationship("ReviewLink")
    session = relationship("ReviewSession")


class TeamVideoRoomMessage(Base):
    """Persistent team chat for internal /player WebSocket room (per video)."""

    __tablename__ = "team_video_room_messages"

    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(
        Integer,
        ForeignKey("videos.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    body = Column(Text, nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)

    video = relationship("Video")
    user = relationship("User")


class ReviewRecordingSession(Base):
    __tablename__ = "review_recording_sessions"

    id = Column(Integer, primary_key=True, index=True)
    review_link_id = Column(
        Integer,
        ForeignKey("review_links.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id = Column(
        Integer,
        ForeignKey("review_sessions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    status = Column(String, server_default="processing", nullable=False)
    file_url = Column(Text, nullable=True)
    storage_key = Column(Text, nullable=True)
    mime_type = Column(String, nullable=True)
    bytes_size = Column(Integer, nullable=True)
    consent_snapshot = Column(JSONB, nullable=True)
    started_at = Column(TIMESTAMP, nullable=True)
    ended_at = Column(TIMESTAMP, nullable=True)
    archived_at = Column(TIMESTAMP, nullable=True)
    deleted_at = Column(TIMESTAMP, nullable=True)
    retention_days = Column(Integer, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    review_link = relationship("ReviewLink")
    session = relationship("ReviewSession")
    creator = relationship("User")

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
    timecode = Column(Float, nullable=False)
    duration = Column(Float, server_default="0.1", nullable=False)
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
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=True)
    workspace_invite_id = Column(Integer, ForeignKey("workspace_invites.id", ondelete="SET NULL"), nullable=True)
    invite_token = Column(String, nullable=True)
    message = Column(Text, nullable=True)
    read = Column(Boolean, default=False)
    created_at = Column(TIMESTAMP, server_default=func.now())

    user = relationship("User")
    project = relationship("Project")
    video = relationship("Video")
    comment = relationship("Comment")
    workspace = relationship("Workspace")
    workspace_invite = relationship("WorkspaceInvite")


class DevicePushToken(Base):
    __tablename__ = "device_push_tokens"
    __table_args__ = (
        UniqueConstraint("token", name="uq_device_push_token_token"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token = Column(String, nullable=False, index=True)
    platform = Column(String, nullable=False)  # ios | android | web
    device_name = Column(String, nullable=True)
    app_version = Column(String, nullable=True)
    enabled = Column(Boolean, server_default="true", nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User")


class CommentAttachment(Base):
    __tablename__ = "comment_attachments"

    id = Column(Integer, primary_key=True, index=True)
    comment_id = Column(Integer, ForeignKey("comments.id", ondelete="CASCADE"), nullable=False, index=True)
    attachment_type = Column(String, nullable=False, index=True)  # voice_note | image | file
    file_url = Column(Text, nullable=False)
    mime_type = Column(String, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    bytes_size = Column(Integer, nullable=True)
    waveform = Column(JSONB, nullable=True)
    transcript = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)

    comment = relationship("Comment", back_populates="attachments")

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


class SecurityAuditLog(Base):
    __tablename__ = "security_audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)
    video_id = Column(Integer, ForeignKey("videos.id", ondelete="SET NULL"), nullable=True, index=True)
    review_link_id = Column(Integer, ForeignKey("review_links.id", ondelete="SET NULL"), nullable=True, index=True)
    session_id = Column(Integer, ForeignKey("review_sessions.id", ondelete="SET NULL"), nullable=True, index=True)
    actor_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    actor_type = Column(String, nullable=False, server_default="system")
    action = Column(String, nullable=False, index=True)
    resource_type = Column(String, nullable=False)
    resource_id = Column(String, nullable=True)
    outcome = Column(String, nullable=False, server_default="success")
    ip_address = Column(String, nullable=True)
    country_code = Column(String, nullable=True, index=True)
    user_agent = Column(Text, nullable=True)
    meta_info = Column("metadata", JSONB, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False, index=True)

    workspace = relationship("Workspace")
    project = relationship("Project")
    video = relationship("Video")
    review_link = relationship("ReviewLink")
    session = relationship("ReviewSession")
    actor = relationship("User")


class NDADocument(Base):
    __tablename__ = "nda_documents"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String, nullable=False)
    version = Column(String, nullable=False)
    body_markdown = Column(Text, nullable=False)
    content_sha256 = Column(String, nullable=False, index=True)
    is_active = Column(Boolean, server_default="true", nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    workspace = relationship("Workspace")
    created_by = relationship("User")


class NDAAcceptance(Base):
    __tablename__ = "nda_acceptances"
    __table_args__ = (
        UniqueConstraint(
            "review_link_id",
            "identity_key",
            "nda_document_id",
            name="uq_nda_acceptance_link_identity_doc",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    review_link_id = Column(Integer, ForeignKey("review_links.id", ondelete="CASCADE"), nullable=False, index=True)
    nda_document_id = Column(Integer, ForeignKey("nda_documents.id", ondelete="CASCADE"), nullable=False, index=True)
    identity_key = Column(String, nullable=False, index=True)
    guest_name = Column(String, nullable=True)
    guest_email = Column(String, nullable=True, index=True)
    ip_address = Column(String, nullable=True)
    user_agent = Column(Text, nullable=True)
    accepted_at = Column(TIMESTAMP, server_default=func.now(), nullable=False, index=True)

    review_link = relationship("ReviewLink")
    nda_document = relationship("NDADocument")


class UserMFAMethod(Base):
    __tablename__ = "user_mfa_methods"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    method_type = Column(String, nullable=False, server_default="totp")
    label = Column(String, nullable=True)
    secret_encrypted = Column(Text, nullable=False)
    is_primary = Column(Boolean, server_default="true", nullable=False)
    verified_at = Column(TIMESTAMP, nullable=True)
    disabled_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User")


class UserMFARecoveryCode(Base):
    __tablename__ = "user_mfa_recovery_codes"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    code_hash = Column(String, nullable=False, unique=True, index=True)
    used_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)

    user = relationship("User")


class WorkspaceSSOProvider(Base):
    __tablename__ = "workspace_sso_providers"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    provider = Column(String, nullable=False)  # google | okta | azure_ad
    issuer = Column(String, nullable=False)
    client_id = Column(String, nullable=False)
    client_secret_encrypted = Column(Text, nullable=False)
    authorization_endpoint = Column(String, nullable=True)
    token_endpoint = Column(String, nullable=True)
    userinfo_endpoint = Column(String, nullable=True)
    jwks_uri = Column(String, nullable=True)
    scope = Column(String, server_default="openid profile email", nullable=False)
    domain_hint = Column(String, nullable=True, index=True)
    enabled = Column(Boolean, server_default="true", nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    workspace = relationship("Workspace")
    created_by = relationship("User")


class WorkspaceAuthPolicy(Base):
    __tablename__ = "workspace_auth_policies"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    enforce_sso = Column(Boolean, server_default="false", nullable=False)
    allowed_login_methods = Column(ARRAY(String), nullable=True)
    mfa_required = Column(Boolean, server_default="false", nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    workspace = relationship("Workspace")


class ReviewForensicAsset(Base):
    __tablename__ = "review_forensic_assets"

    id = Column(Integer, primary_key=True, index=True)
    review_link_id = Column(Integer, ForeignKey("review_links.id", ondelete="CASCADE"), nullable=False, index=True)
    review_session_id = Column(Integer, ForeignKey("review_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    watermark_fingerprint = Column(String, nullable=False, index=True)
    playback_manifest_url = Column(Text, nullable=True)
    package_status = Column(String, nullable=False, server_default="pending")
    package_metadata = Column(JSONB, nullable=True)
    expires_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    review_link = relationship("ReviewLink")
    review_session = relationship("ReviewSession")


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


class DeliveryExport(Base):
    __tablename__ = "delivery_exports"

    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(Integer, ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True)
    profile_key = Column(String, nullable=False, index=True)  # 4k_master | yt_1080p | social_720p
    status = Column(String, server_default="pending", nullable=False)  # pending|queued|processing|completed|failed
    output_path = Column(String, nullable=True)
    mime_type = Column(String, nullable=True)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    size_bytes = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    video = relationship("Video")
    creator = relationship("User")


class DeliveryPackage(Base):
    __tablename__ = "delivery_packages"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    video_id = Column(Integer, ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String, server_default="pending", nullable=False)  # pending|queued|processing|completed|failed
    requested_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approved_version_id = Column(Integer, ForeignKey("videos.id", ondelete="SET NULL"), nullable=True)
    zip_url = Column(String, nullable=True)
    zip_size_bytes = Column(Integer, nullable=True)
    checksum_sha256 = Column(String, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    completed_at = Column(TIMESTAMP, nullable=True)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    project = relationship("Project")
    video = relationship("Video", foreign_keys=[video_id])
    approved_version = relationship("Video", foreign_keys=[approved_version_id])
    requested_by = relationship("User")
    assets = relationship("DeliveryAsset", back_populates="delivery_package", cascade="all, delete-orphan")
    links = relationship("DeliveryLink", back_populates="delivery_package", cascade="all, delete-orphan")


class DeliveryAsset(Base):
    __tablename__ = "delivery_assets"

    id = Column(Integer, primary_key=True, index=True)
    delivery_package_id = Column(
        Integer, ForeignKey("delivery_packages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_type = Column(String, nullable=False, index=True)
    file_url = Column(String, nullable=False)
    filename = Column(String, nullable=False)
    mime_type = Column(String, nullable=True)
    size_bytes = Column(Integer, nullable=True)
    checksum_sha256 = Column(String, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)

    delivery_package = relationship("DeliveryPackage", back_populates="assets")
    receipts = relationship("DeliveryReceipt", back_populates="delivery_asset", cascade="all, delete-orphan")


class DeliveryLink(Base):
    __tablename__ = "delivery_links"

    id = Column(Integer, primary_key=True, index=True)
    delivery_package_id = Column(
        Integer, ForeignKey("delivery_packages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token = Column(String, unique=True, index=True, nullable=False)
    expires_at = Column(TIMESTAMP, nullable=False, index=True)
    renew_count = Column(Integer, server_default="0", nullable=False)
    is_revoked = Column(Boolean, server_default="false", nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    last_renewed_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)

    delivery_package = relationship("DeliveryPackage", back_populates="links")
    created_by = relationship("User")
    receipts = relationship("DeliveryReceipt", back_populates="delivery_link", cascade="all, delete-orphan")


class DeliveryReceipt(Base):
    __tablename__ = "delivery_receipts"

    id = Column(Integer, primary_key=True, index=True)
    delivery_link_id = Column(Integer, ForeignKey("delivery_links.id", ondelete="CASCADE"), nullable=False, index=True)
    delivery_asset_id = Column(Integer, ForeignKey("delivery_assets.id", ondelete="CASCADE"), nullable=True, index=True)
    downloaded_at = Column(TIMESTAMP, server_default=func.now(), nullable=False, index=True)
    session_id = Column(String, nullable=True)
    guest_name = Column(String, nullable=True)
    guest_email = Column(String, nullable=True)
    ip_address = Column(String, nullable=True)
    user_agent = Column(Text, nullable=True)

    delivery_link = relationship("DeliveryLink", back_populates="receipts")
    delivery_asset = relationship("DeliveryAsset", back_populates="receipts")


class ProjectRetentionPolicy(Base):
    __tablename__ = "project_retention_policies"

    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    auto_archive_enabled = Column(Boolean, server_default="true", nullable=False)
    archive_after_days = Column(Integer, server_default="90", nullable=False)
    cold_tier_provider = Column(String, nullable=True)
    last_archive_run_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    project = relationship("Project")


class ProjectArchiveState(Base):
    __tablename__ = "project_archive_states"

    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    state = Column(String, server_default="active", nullable=False)  # active|archived|cold_storage
    archived_at = Column(TIMESTAMP, nullable=True)
    cold_moved_at = Column(TIMESTAMP, nullable=True)
    storage_location_meta = Column(JSONB, nullable=True)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    project = relationship("Project")


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


# =====================================================================
# Editor Integration models (NLE sync, proxies, watch folders)
# =====================================================================


class VideoProxy(Base):
    """Proxy renditions of original video uploads for fast NLE review."""

    __tablename__ = "video_proxies"

    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(Integer, ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True)
    profile = Column(String, nullable=False, index=True)  # 540p_h264 | 720p_h264 | 1080p_h264
    status = Column(String, server_default="pending", nullable=False)  # pending|processing|completed|failed
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    bitrate_kbps = Column(Integer, nullable=True)
    codec = Column(String, nullable=True)
    file_url = Column(String, nullable=True)
    file_path_local = Column(String, nullable=True)
    size_bytes = Column(Integer, nullable=True)
    duration = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    video = relationship("Video", back_populates="proxies")


class WatchFolderConfig(Base):
    """Per-user/project watch folder settings for auto-upload."""

    __tablename__ = "watch_folder_configs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    folder_path = Column(String, nullable=False)
    auto_proxy = Column(Boolean, server_default="true", nullable=False)
    auto_version = Column(Boolean, server_default="true", nullable=False)
    file_pattern = Column(String, server_default="*", nullable=False)  # glob, e.g. "*.mp4,*.mov"
    last_sync_at = Column(TIMESTAMP, nullable=True)
    is_active = Column(Boolean, server_default="true", nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User")
    project = relationship("Project")


class NLESession(Base):
    """Tracks active NLE integration connections (Premiere, Resolve, FCP X, AE)."""

    __tablename__ = "nle_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    nle_type = Column(String, nullable=False, index=True)  # premiere|resolve|fcpx|after_effects
    nle_version = Column(String, nullable=True)
    host_name = Column(String, nullable=True)
    last_sync_at = Column(TIMESTAMP, nullable=True)
    sync_direction = Column(String, server_default="bidirectional", nullable=False)  # push|pull|bidirectional
    is_active = Column(Boolean, server_default="true", nullable=False)
    extra = Column(JSONB, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User")
    project = relationship("Project")


# =====================================================================
# COMMUNITY FORUM MODELS (Schema: community)
# =====================================================================

class ForumCategory(Base):
    __tablename__ = "categories"
    __table_args__ = {"schema": "community"}

    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    color = Column(String, nullable=True, server_default="#8B5CF6")
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)

    posts = relationship("ForumPost", back_populates="category", cascade="all, delete-orphan")


class ForumPost(Base):
    __tablename__ = "posts"
    __table_args__ = {"schema": "community"}

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    status = Column(String, server_default="open", index=True, nullable=False) # open, planned, in_progress, completed, closed
    view_count = Column(Integer, server_default="0", nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    category_id = Column(Integer, ForeignKey("community.categories.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User")
    category = relationship("ForumCategory", back_populates="posts")
    comments = relationship("ForumComment", back_populates="post", cascade="all, delete-orphan")
    votes = relationship("ForumVote", back_populates="post", cascade="all, delete-orphan")


class ForumComment(Base):
    __tablename__ = "comments"
    __table_args__ = {"schema": "community"}

    id = Column(Integer, primary_key=True, index=True)
    content = Column(Text, nullable=False)
    post_id = Column(Integer, ForeignKey("community.posts.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    parent_id = Column(Integer, ForeignKey("community.comments.id", ondelete="CASCADE"), nullable=True, index=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    post = relationship("ForumPost", back_populates="comments")
    user = relationship("User")
    parent = relationship("ForumComment", remote_side=[id], back_populates="replies")
    replies = relationship("ForumComment", back_populates="parent", cascade="all, delete-orphan")


class ForumVote(Base):
    __tablename__ = "votes"
    __table_args__ = (
        UniqueConstraint("post_id", "user_id", name="uq_forum_votes_post_user"),
        {"schema": "community"}
    )

    id = Column(Integer, primary_key=True, index=True)
    post_id = Column(Integer, ForeignKey("community.posts.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)

    post = relationship("ForumPost", back_populates="votes")


# ---------------------------------------------------------------------------
# Repurpose (OpusClip-style short-clip pipeline)
# ---------------------------------------------------------------------------


class Clip(Base):
    """A short clip cut from a source video for social repurposing."""

    __tablename__ = "clips"
    __table_args__ = {"schema": "repurpose"}

    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(
        Integer, ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name = Column(String, nullable=False, server_default="Untitled clip")
    start_time = Column(Float, nullable=False)
    end_time = Column(Float, nullable=False)
    duration_seconds = Column(Float, nullable=True)
    aspect_ratio = Column(String, nullable=False, server_default="9:16")
    virality_score = Column(Float, nullable=True)
    status = Column(String, nullable=False, server_default="draft", index=True)
    render_progress = Column(Integer, nullable=False, server_default="0")
    render_error = Column(Text, nullable=True)
    storage_path = Column(String, nullable=True)
    thumbnail_url = Column(String, nullable=True)
    transcript_text = Column(Text, nullable=True)
    is_ai_suggested = Column(Boolean, nullable=False, server_default="false")
    suggestion_reason = Column(Text, nullable=True)
    hooks_matched = Column(JSONB, nullable=True)
    preset = Column(String, nullable=True)
    rq_job_id = Column(String, nullable=True)
    completed_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(
        TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    video = relationship("Video")
    user = relationship("User")
    style = relationship(
        "ClipStyle",
        back_populates="clip",
        uselist=False,
        cascade="all, delete-orphan",
    )


class ClipStyle(Base):
    """Caption/motion style for a clip (1:1 with Clip)."""

    __tablename__ = "clip_styles"
    __table_args__ = {"schema": "repurpose"}

    id = Column(Integer, primary_key=True, index=True)
    clip_id = Column(
        Integer,
        ForeignKey("repurpose.clips.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    caption_enabled = Column(Boolean, nullable=False, server_default="true")
    caption_font = Column(String, nullable=False, server_default="Inter")
    caption_size = Column(Integer, nullable=False, server_default="56")
    caption_color = Column(String, nullable=False, server_default="#FFFFFF")
    caption_bg_color = Column(String, nullable=True)
    caption_position = Column(String, nullable=False, server_default="bottom")
    caption_animation = Column(String, nullable=True)
    caption_max_words = Column(Integer, nullable=False, server_default="4")
    caption_words_per_line = Column(Integer, nullable=False, server_default="3")
    caption_max_lines = Column(Integer, nullable=False, server_default="2")
    caption_highlight_color = Column(String, nullable=False, server_default="#FACC15")
    caption_highlight_style = Column(String, nullable=False, server_default="color")
    caption_stroke_color = Column(String, nullable=True, server_default="#000000")
    caption_stroke_width = Column(Integer, nullable=False, server_default="3")
    caption_font_weight = Column(String, nullable=False, server_default="700")
    caption_position_y = Column(Float, nullable=True, server_default="85")
    caption_position_x = Column(Float, nullable=True, server_default="50")
    caption_uppercase = Column(Boolean, nullable=False, server_default="false")
    brand_logo_url = Column(String, nullable=True)
    brand_logo_position = Column(String, nullable=True, server_default="top-right")
    brand_logo_scale = Column(Float, nullable=False, server_default="0.12")
    brand_watermark_opacity = Column(Float, nullable=False, server_default="0.85")
    background_music_url = Column(String, nullable=True)
    background_music_volume = Column(Float, nullable=False, server_default="0.25")
    original_audio_volume = Column(Float, nullable=False, server_default="1.0")
    video_keyframes = Column(JSONB, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(
        TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    clip = relationship("Clip", back_populates="style")


class ClipTemplate(Base):
    """Reusable caption/motion preset. user_id NULL = global built-in."""

    __tablename__ = "clip_templates"
    __table_args__ = {"schema": "repurpose"}

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name = Column(String, nullable=False)
    category = Column(String, nullable=False, server_default="social")
    is_public = Column(Boolean, nullable=False, server_default="false")
    preview_url = Column(String, nullable=True)
    style_config = Column(JSONB, nullable=False)
    usage_count = Column(Integer, nullable=False, server_default="0")
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(
        TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user = relationship("User")


class RepurposeJob(Base):
    __tablename__ = "repurpose_jobs"
    __table_args__ = {"schema": "repurpose"}

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)
    video_id = Column(Integer, ForeignKey("videos.id", ondelete="SET NULL"), nullable=True)
    source_mode = Column(String, nullable=False)  # youtube_url | upload | project_video
    source_url = Column(Text, nullable=True)
    source_file_url = Column(Text, nullable=True)
    source_title = Column(String, nullable=True)
    source_meta = Column(JSONB, nullable=True)
    clip_mode = Column(String, nullable=False, server_default="basic")
    clip_anything_prompt = Column(Text, nullable=True)
    genres = Column(JSONB, nullable=True)
    clip_length_bucket = Column(String, nullable=False)
    subtitle_template_id = Column(Integer, nullable=True)
    aspect_ratio = Column(String, nullable=False, server_default="9:16")
    source_trim_seconds = Column(Integer, nullable=True)
    status = Column(String, nullable=False, server_default="queued")
    created_clip_ids = Column(JSONB, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User")
    project = relationship("Project")
    video = relationship("Video")


class RepurposeUserDefaults(Base):
    __tablename__ = "user_defaults"
    __table_args__ = {"schema": "repurpose"}

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True, unique=True)
    clip_mode = Column(String, nullable=False, server_default="basic")
    default_prompt = Column(Text, nullable=True)
    genres = Column(JSONB, nullable=True)
    clip_length_bucket = Column(String, nullable=False, server_default="lt_30")
    subtitle_template_id = Column(Integer, nullable=True)
    aspect_ratio = Column(String, nullable=False, server_default="9:16")
    source_trim_seconds = Column(Integer, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User")