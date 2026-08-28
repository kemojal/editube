from sqlalchemy import Column, Integer, BigInteger, Float, String, ForeignKey, Text, Boolean, ARRAY, LargeBinary, UniqueConstraint, Index, text
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
    # Retired. Held one org type ("agency", "freelancer", "internal") and later
    # one workflow. Kept for the historical answers only — nothing writes it.
    workflow_type = Column(String, nullable=True)
    # The workflows the user picked in onboarding, in the order they picked
    # them: any of "auto_edit", "repurpose", "review". A list because these are
    # not exclusive — most people arrive wanting more than one.
    workflow_types = Column(JSONB, nullable=True)
    # The tier the account is *entitled* to. Every quota in the app reads this
    # — storage caps, UGC credits, seat caps — so it is only ever written from
    # a Stripe subscription state, never from user input.
    plan = Column(String, nullable=True)  # "free", "pro", "scale", "enterprise"
    # The tier the user picked in onboarding, before paying for it. Kept apart
    # from `plan` because `PUT /users/onboarding/plan` used to write `plan`
    # directly, which let any authenticated account award itself Scale quotas
    # without touching Stripe.
    selected_plan = Column(String, nullable=True)
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
    # Set when the user self-deletes their account; PII is anonymized and all
    # sessions/tokens revoked. A non-null value means the account is deactivated.
    deleted_at = Column(TIMESTAMP, nullable=True)
    # Set atomically by the analytics endpoint the first time the authenticated
    # dashboard renders. This makes the activation event cross-device safe.
    first_dashboard_viewed_at = Column(TIMESTAMP, nullable=True)
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
    google_drive_connections = relationship(
        "UserGoogleDriveConnection",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    workspace_memberships = relationship(
        "WorkspaceMember",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    api_tokens = relationship(
        "ApiToken",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    zoom_connection = relationship(
        "UserZoomConnection",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )
    referral_code = relationship(
        "ReferralCode",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
        foreign_keys="ReferralCode.user_id",
    )
    #: Referrals this user *sent*. Referral has two FKs to users, so the join
    #: has to name which one.
    referrals_made = relationship(
        "Referral",
        back_populates="referrer",
        cascade="all, delete-orphan",
        foreign_keys="Referral.referrer_user_id",
    )


class UserZoomConnection(Base):
    """A user's connected Zoom account (OAuth), for importing cloud recordings."""

    __tablename__ = "user_zoom_connections"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False, index=True
    )
    zoom_user_id = Column(String, nullable=False)
    email = Column(String, nullable=True)
    display_name = Column(String, nullable=True)
    # Fernet-encrypted refresh token; access token is short-lived and refreshed.
    refresh_token_encrypted = Column(Text, nullable=False)
    access_token = Column(Text, nullable=True)
    access_expires_at = Column(TIMESTAMP, nullable=True)
    status = Column(String, server_default="active", nullable=False)  # active | revoked
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User", back_populates="zoom_connection")


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
    # Whether the user opts in to sharing usage data to improve the product.
    share_data = Column(Boolean, server_default="false", nullable=False)
    # Default visibility applied to newly published work: private | link | workspace.
    default_publish_privacy = Column(String, server_default="private", nullable=False)
    # Per-capability default AI model choices, e.g. {"transcription": "base",
    # "editing": "gemini-3-flash-preview", "image": "...", "video": "..."}.
    ai_model_preferences = Column(JSONB, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User", back_populates="settings")


class ApiToken(Base):
    """Personal access token for programmatic API access (format: ``edt_<hex>``)."""

    __tablename__ = "api_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String, nullable=False)
    # First chars of the raw token kept for display, e.g. "edt_1a2b3c4d".
    token_prefix = Column(String, nullable=False)
    # SHA-256 hex of the full raw token; the raw token is shown once and never stored.
    token_hash = Column(String, unique=True, index=True, nullable=False)
    last_used_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)

    user = relationship("User", back_populates="api_tokens")


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
    cancellation_requested_at = Column(TIMESTAMP, nullable=True)
    cancellation_effective_at = Column(TIMESTAMP, nullable=True)
    cancellation_feedback = Column(String, nullable=True)
    cancellation_comment_encrypted = Column(Text, nullable=True)
    cancellation_source = Column(String, nullable=True)
    voluntary_churn = Column(Boolean, nullable=True)
    currency = Column(String, nullable=True)
    unit_amount = Column(BigInteger, nullable=True)
    quantity = Column(Integer, nullable=True)
    discount_amount = Column(BigInteger, nullable=True)
    discount_percent = Column(Float, nullable=True)
    recurring_interval = Column(String, nullable=True)
    latest_invoice_id = Column(String, nullable=True, index=True)
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


class StripeWebhookEvent(Base):
    """One row per Stripe event id that has been fully processed.

    Stripe guarantees at-least-once delivery and retries any webhook that does
    not return 2xx, so every handler must be idempotent. Most of them were —
    upserts keyed on the subscription id — but the side effects were not: a
    single retried ``checkout.session.completed`` sent the welcome email again,
    and a retried ``customer.subscription.updated`` re-sent the
    "will not renew" notice. The insert of this row is what makes a replay a
    no-op, so it is committed only after the handler has succeeded.
    """

    __tablename__ = "stripe_webhook_events"

    id = Column(Integer, primary_key=True, index=True)
    stripe_event_id = Column(String, unique=True, index=True, nullable=False)
    event_type = Column(String, nullable=True)
    processed_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)


class AnalyticsOutbox(Base):
    """Transactional product-event delivery queue.

    Authoritative events are inserted in the same transaction as the state
    change they describe. A worker delivers them to the configured analytics
    provider; provider downtime never sits on a customer request path.
    """

    __tablename__ = "analytics_outbox"

    event_id = Column(String, primary_key=True)
    event_name = Column(String, nullable=False, index=True)
    schema_version = Column(Integer, nullable=False, default=1, server_default="1")
    occurred_at = Column(TIMESTAMP, nullable=False, index=True)
    source = Column(String, nullable=False)
    environment = Column(String, nullable=False)
    release = Column(String, nullable=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    workspace_id = Column(
        Integer, ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True, index=True
    )
    anonymous_id = Column(String, nullable=True)
    properties = Column(JSONB, nullable=False)
    delivery_status = Column(String, nullable=False, server_default="pending", index=True)
    attempt_count = Column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at = Column(TIMESTAMP, nullable=True, index=True)
    delivery_started_at = Column(TIMESTAMP, nullable=True)
    last_error_code = Column(String, nullable=True)
    delivered_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)


class CheckoutAttempt(Base):
    """First-party ledger used to model matured checkout abandonment safely."""

    __tablename__ = "checkout_attempts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    workspace_id = Column(
        Integer, ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True, index=True
    )
    stripe_checkout_session_id = Column(String, unique=True, nullable=False, index=True)
    plan = Column(String, nullable=False)
    recurring_interval = Column(String, nullable=False)
    campaign_id = Column(String, nullable=True)
    source = Column(String, nullable=False, server_default="billing_checkout")
    trial_days = Column(Integer, nullable=False, server_default="0")
    offer_applied = Column(Boolean, nullable=False, server_default="false")
    status = Column(String, nullable=False, server_default="created", index=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False, index=True)
    completed_at = Column(TIMESTAMP, nullable=True)
    canceled_at = Column(TIMESTAMP, nullable=True)
    abandoned_at = Column(TIMESTAMP, nullable=True)


class AnalyticsConsent(Base):
    """Auditable analytics/replay/content-improvement preference history."""

    __tablename__ = "analytics_consents"
    __table_args__ = (
        UniqueConstraint("anonymous_consent_id", name="uq_analytics_consent_anonymous"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    anonymous_consent_id = Column(String, nullable=False)
    consent_state = Column(String, nullable=False, server_default="essential_only")
    analytics_enabled = Column(Boolean, nullable=False, server_default="false")
    replay_enabled = Column(Boolean, nullable=False, server_default="false")
    product_data_improvement_enabled = Column(
        Boolean, nullable=False, server_default="false"
    )
    consent_version = Column(String, nullable=False)
    region_policy = Column(String, nullable=False, server_default="default")
    global_privacy_control = Column(Boolean, nullable=False, server_default="false")
    consented_at = Column(TIMESTAMP, nullable=True)
    withdrawn_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(
        TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AnalyticsConsentEvent(Base):
    """Append-only evidence for each consent grant, rejection, or withdrawal."""

    __tablename__ = "analytics_consent_events"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    anonymous_consent_id = Column(String, nullable=False, index=True)
    consent_state = Column(String, nullable=False)
    analytics_enabled = Column(Boolean, nullable=False)
    replay_enabled = Column(Boolean, nullable=False)
    product_data_improvement_enabled = Column(Boolean, nullable=False)
    consent_version = Column(String, nullable=False)
    region_policy = Column(String, nullable=False)
    global_privacy_control = Column(Boolean, nullable=False)
    occurred_at = Column(TIMESTAMP, server_default=func.now(), nullable=False, index=True)


class AnalyticsFeedback(Base):
    """Restricted qualitative evidence; free text never enters PostHog."""

    __tablename__ = "analytics_feedback"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    workspace_id = Column(
        Integer, ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True, index=True
    )
    prompt_key = Column(String, nullable=False, index=True)
    reason_code = Column(String, nullable=False, index=True)
    comment_encrypted = Column(Text, nullable=True)
    route_template = Column(String, nullable=True)
    feature_key = Column(String, nullable=True, index=True)
    analytics_session_id = Column(String, nullable=True)
    consent_version = Column(String, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False, index=True)


class AnalyticsDataRequest(Base):
    """Auditable export/deletion request, including provider completion state."""

    __tablename__ = "analytics_data_requests"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    distinct_id = Column(String, nullable=False, index=True)
    request_type = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, server_default="pending", index=True)
    provider_status = Column(JSONB, nullable=True)
    last_error_code = Column(String, nullable=True)
    requested_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    completed_at = Column(TIMESTAMP, nullable=True)


class SubscriptionLifecycleEvent(Base):
    """Append-only Stripe subscription/revenue history for cohort metrics."""

    __tablename__ = "subscription_lifecycle_events"

    id = Column(Integer, primary_key=True, index=True)
    event_key = Column(String, unique=True, nullable=False, index=True)
    event_type = Column(String, nullable=False, index=True)
    source_event_id = Column(String, nullable=True, index=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    workspace_id = Column(
        Integer, ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True, index=True
    )
    stripe_subscription_id = Column(String, nullable=True, index=True)
    stripe_invoice_id = Column(String, nullable=True, index=True)
    plan = Column(String, nullable=True)
    previous_plan = Column(String, nullable=True)
    status = Column(String, nullable=True)
    previous_status = Column(String, nullable=True)
    currency = Column(String, nullable=True)
    amount_minor = Column(BigInteger, nullable=True)
    quantity = Column(Integer, nullable=True)
    recurring_interval = Column(String, nullable=True)
    voluntary = Column(Boolean, nullable=True)
    reason_code = Column(String, nullable=True)
    effective_at = Column(TIMESTAMP, nullable=True)
    meta_info = Column(JSONB, nullable=True)
    occurred_at = Column(TIMESTAMP, nullable=False, index=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)


class WorkspaceActivation(Base):
    """The first durable value moment for a workspace, exactly once."""

    __tablename__ = "workspace_activations"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(
        Integer,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    feature_key = Column(String, nullable=False, index=True)
    resource_type = Column(String, nullable=True)
    resource_id = Column(String, nullable=True)
    achieved_at = Column(TIMESTAMP, server_default=func.now(), nullable=False, index=True)


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
    #: Object key when the file lives in R2/Cloudinary; NULL for local disk.
    storage_key = Column(String, nullable=True)
    # Media metadata, so the asset browser can render a grid (and the storage
    # meter can count these bytes) without opening every file.
    mime_type = Column(String, nullable=True)
    # BigInteger: Integer overflows at ~2.1 GB and b-roll routinely exceeds it.
    size_bytes = Column(BigInteger, server_default="0", nullable=False)
    duration_ms = Column(Integer, nullable=True)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    thumbnail_url = Column(String, nullable=True)
    extra = Column(JSONB, nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)

    workspace = relationship("Workspace", back_populates="assets")
    created_by = relationship("User", foreign_keys=[created_by_user_id])
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
    project_type = Column(String, nullable=True, index=True)  # "rough-cut", "review", "repurpose"
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
    # Groups videos that are versions of the same deliverable. `version` is the
    # per-group ordinal (v1, v2, …). NULL only transiently before backfill.
    version_group_id = Column(String, nullable=True, index=True)
    file_path = Column(Text, nullable=False)
    # Stable, non-sensitive origin used for lifecycle analytics and ingest QA.
    # Never store a local watch-folder path here.
    ingest_source = Column(String, nullable=True, index=True)
    ingest_page_url = Column(Text, nullable=True)
    thumbnail_url = Column(String, nullable=True)
    status = Column(String, server_default="in_progress", nullable=False)  # in_progress, in_review, approved, needs_changes
    # Written only by app.services.video_status.apply_video_status.
    status_changed_at = Column(TIMESTAMP, nullable=True)
    status_changed_by = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # When reviewers are expected to have responded by. Set by "send for review".
    review_due_at = Column(TIMESTAMP, nullable=True)
    # "What changed in this version" — shown to reviewers above the comment feed.
    version_notes = Column(Text, nullable=True)
    duration = Column(Integer, nullable=True)  # duration in seconds
    size_bytes = Column(Integer, server_default="0", nullable=False)
    uploader_id = Column(Integer, ForeignKey("users.id"))
    uploader = relationship("User", foreign_keys=[uploader_id])
    status_actor = relationship("User", foreign_keys=[status_changed_by])
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
    approvals = relationship(
        "VideoApproval",
        back_populates="video",
        foreign_keys="VideoApproval.video_id",
        cascade="all, delete-orphan",
        order_by="VideoApproval.created_at.desc()",
    )


class VideoApproval(Base):
    """One review decision on one version.

    `Video.status` answers "where is this cut now"; these rows answer "who
    decided what, on which version, and was that decision later superseded".
    Team decisions (actor_user_id) and guest decisions (review_session_id) land
    in the same table on purpose — that convergence is what makes approval a
    single mechanism rather than four disconnected ones.
    """

    __tablename__ = "video_approvals"

    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(
        Integer, ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # approved | changes_requested
    decision = Column(String, nullable=False, index=True)
    actor_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    review_session_id = Column(
        Integer, ForeignKey("review_sessions.id", ondelete="SET NULL"), nullable=True
    )
    review_link_id = Column(
        Integer, ForeignKey("review_links.id", ondelete="SET NULL"), nullable=True
    )
    note = Column(Text, nullable=True)
    # Stamped when a newer version lands, so history reads correctly six months
    # later ("v2 was approved, then superseded by v3").
    superseded_at = Column(TIMESTAMP, nullable=True)
    superseded_by_video_id = Column(
        Integer, ForeignKey("videos.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)

    video = relationship("Video", back_populates="approvals", foreign_keys=[video_id])
    actor = relationship("User", foreign_keys=[actor_user_id])
    review_session = relationship("ReviewSession", foreign_keys=[review_session_id])


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
    # Silero-VAD speech/silence ranges over the extracted audio, in source
    # seconds: {version, engine, sample_rate, duration, speech_ranges,
    # silences, params}. Written by app.jobs.transcription; consumed by
    # auto-edit silence suggestions and the editor's pause UI.
    audio_analysis = Column(JSONB, nullable=True)
    error_message = Column(Text, nullable=True)
    model_name = Column(String, nullable=True)
    # User-requested spoken language (ISO 639-1, e.g. "en"). NULL = auto-detect.
    language = Column(String, nullable=True)
    # Language Whisper actually detected (persisted even in auto-detect mode).
    detected_language = Column(String, nullable=True)
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
    # Set when this comment was copied onto a new version because it was still
    # open. Points at the original on the previous version, which stays put as
    # history — carry-forward copies, it never moves.
    carried_from_comment_id = Column(
        Integer, ForeignKey("comments.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    video = relationship("Video", back_populates="comments")
    user = relationship("User", foreign_keys=[user_id])
    assignee = relationship("User", foreign_keys=[assignee_user_id])
    # Two self-referential foreign keys now exist (parent_id and
    # carried_from_comment_id), so every self-relationship must name its own.
    parent = relationship(
        "Comment", remote_side=[id], back_populates="replies", foreign_keys=[parent_id]
    )
    carried_from = relationship(
        "Comment", remote_side=[id], foreign_keys=[carried_from_comment_id]
    )
    replies = relationship(
        "Comment",
        back_populates="parent",
        foreign_keys=[parent_id],
        cascade="all, delete-orphan",
    )
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
    analytics_milestones = Column(JSONB, server_default="[]", nullable=False)

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

    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="uq_review_events_session_seq"),
    )

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
    read_at = Column(TIMESTAMP, nullable=True)
    # Who caused this, so the UI can name them without re-fetching the comment.
    actor_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Siblings sharing a key inside the coalescing window fold into one row
    # rather than inserting twenty. See app/services/notifications.py.
    group_key = Column(String, nullable=True, index=True)
    group_count = Column(Integer, server_default="1", nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now())

    user = relationship("User", foreign_keys=[user_id])
    actor = relationship("User", foreign_keys=[actor_user_id])
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
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"))
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
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


class UserGoogleDriveConnection(Base):
    """Google Drive OAuth tokens, scoped to ``drive.file``.

    Deliberately NOT folded into ``user_youtube_connections``: that table has a
    UNIQUE ``user_id`` (one Google account per user, forever), while Drive users
    routinely have a personal and a brand/client account. Uniqueness here is
    ``(user_id, google_sub)`` so several accounts can coexist.

    ``drive.file`` only ever grants access to files the user explicitly picked
    through the Google Picker, so this connection cannot enumerate or read the
    rest of their Drive. See ``docs/google-drive-import-plan.md`` §1.
    """

    __tablename__ = "user_google_drive_connections"
    __table_args__ = (
        UniqueConstraint("user_id", "google_sub", name="uq_google_drive_conn_user_sub"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    google_sub = Column(String, nullable=False)
    email = Column(String, nullable=True)
    picture_url = Column(String, nullable=True)
    refresh_token_encrypted = Column(Text, nullable=False)
    # Access token is short-lived and re-mintable from the refresh token, so it
    # is stored plaintext — same convention as UserYoutubeConnection.
    access_token = Column(Text, nullable=True)
    access_expires_at = Column(TIMESTAMP, nullable=True)
    scopes = Column(Text, nullable=True)
    status = Column(String, server_default="active", nullable=False)  # active | revoked
    is_default = Column(Boolean, server_default="false", nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User", back_populates="google_drive_connections")


class DriveImport(Base):
    """One Google Drive file being pulled into our storage.

    Project-independent on purpose: the create-project wizard only creates the
    project at submit, so this mirrors the stateless ``POST /upload/video``
    contract and produces a ``file_path`` the wizard hands to
    ``POST /projects/{id}/videos/from-upload``.
    """

    __tablename__ = "drive_imports"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    connection_id = Column(
        Integer, ForeignKey("user_google_drive_connections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    drive_file_id = Column(String, nullable=False, index=True)
    file_name = Column(String, nullable=True)
    mime_type = Column(String, nullable=True)
    # BigInteger: Integer overflows at ~2.1 GB and video files routinely exceed it.
    total_bytes = Column(BigInteger, server_default="0", nullable=False)
    bytes_transferred = Column(BigInteger, server_default="0", nullable=False)
    progress_percent = Column(Integer, server_default="0", nullable=False)
    duration_seconds = Column(Integer, nullable=True)
    thumbnail_url = Column(String, nullable=True)
    # queued | downloading | uploading | completed | failed | canceled
    status = Column(String, server_default="queued", nullable=False, index=True)
    file_path = Column(Text, nullable=True)
    error_code = Column(String, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User")
    connection = relationship("UserGoogleDriveConnection")


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
    # Ordered kept ranges on the source timeline. Invariant:
    # first.start == start_time, last.end == end_time, sum(end-start) == duration_seconds.
    # Transcript-based deletions shrink or split entries; renderer concats these.
    cuts = Column(JSONB, nullable=False, server_default="[]")
    duration_seconds = Column(Float, nullable=True)
    aspect_ratio = Column(String, nullable=False, server_default="9:16")
    virality_score = Column(Float, nullable=True)
    status = Column(String, nullable=False, server_default="draft", index=True)
    render_progress = Column(Integer, nullable=False, server_default="0")
    render_error = Column(Text, nullable=True)
    storage_path = Column(String, nullable=True)
    thumbnail_url = Column(String, nullable=True)
    transcript_text = Column(Text, nullable=True)
    transcript_highlights = Column(JSONB, nullable=False, server_default="[]")
    transcript_comments = Column(JSONB, nullable=False, server_default="[]")
    edit_history = Column(JSONB, nullable=False, server_default="[]")
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
    caption_template_id = Column(String, nullable=True)
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


class CaptionTemplateDef(Base):
    """A caption template for the rough-cut editor's template gallery.

    The catalogue started life as ``CAPTION_TEMPLATES`` in the frontend; this
    table is the editable source of truth so internal users can refine, add and
    archive templates without a deploy. ``patch`` holds the ``Partial<CaptionStyle>``
    the frontend applies over its caption defaults — the backend treats it as
    opaque JSON, exactly like the rough-cut draft's ``captionStyle``.
    """

    __tablename__ = "caption_templates"

    id = Column(Integer, primary_key=True, index=True)
    # The stable identifier the editor persists in drafts and favorites
    # (`CaptionStyle.templateId`, `user_caption_favorites.template_id`).
    slug = Column(String(80), nullable=False, unique=True, index=True)
    category = Column(String(24), nullable=False, server_default="new")
    label = Column(String(120), nullable=False)
    # Words drawn on the gallery preview card.
    sample = Column(String(200), nullable=False, server_default="")
    # Optional badge: "New" | "Pro" | "Hot" | "Premium".
    tag = Column(String(40), nullable=True)
    blurb = Column(Text, nullable=False, server_default="")
    patch = Column(JSONB, nullable=False, server_default="{}")
    sort_order = Column(Integer, nullable=False, server_default="0")
    # Archived templates disappear from the user-facing gallery but stay
    # resolvable, so drafts that reference them keep rendering.
    archived = Column(Boolean, nullable=False, server_default="false")
    # True for rows seeded from the frontend's built-in catalogue.
    builtin = Column(Boolean, nullable=False, server_default="false")
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(
        TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class UserCaptionFavorite(Base):
    """User-scoped favorites for caption template ids exposed by the rough-cut editor.

    `template_id` is a free-form slug (matches CAPTION_TEMPLATES in the frontend);
    we intentionally do not FK to a templates table so built-in/global templates
    can be referenced without seeding a row per user.
    """

    __tablename__ = "user_caption_favorites"
    __table_args__ = (
        UniqueConstraint("user_id", "template_id", name="uq_user_caption_favorites_user_template"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    template_id = Column(String(80), nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)

    user = relationship("User")


# ---------------------------------------------------------------------------
# AI UGC Ads (``aiugc`` schema) — paste a product URL → AI creator-style ad
# variations. Mirrors the ``repurpose.*`` domain: dedicated Postgres schema,
# JSONB payloads, and Clip-style render-lifecycle columns on UgcVariation.
# ---------------------------------------------------------------------------


class GeneratedMedia(Base):
    """An image or video generated by AI and stored as a project asset.

    Generation is asynchronous (Veo video jobs run for minutes), so a row is
    created immediately in ``pending`` and the worker advances it through
    ``running`` to ``ready``/``failed``. The frontend renders the row as a tile
    in the media panel the whole time, which is what makes progress visible.

    Bytes live in object storage (R2 when ``STORAGE_BACKEND=r2``); this table
    holds the metadata and the resulting URL.
    """

    __tablename__ = "generated_media"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(
        Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The editor session it was generated from; kept for provenance, and null
    # once the video is removed so the asset survives in the project.
    video_id = Column(Integer, ForeignKey("videos.id", ondelete="SET NULL"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    kind = Column(String, nullable=False, index=True)  # image|video
    prompt = Column(Text, nullable=False)
    model = Column(String, nullable=True)
    aspect_ratio = Column(String, nullable=True)
    duration_seconds = Column(Float, nullable=True)

    status = Column(String, nullable=False, server_default="pending", index=True)  # pending|running|ready|failed|cancelled
    progress = Column(Integer, nullable=False, server_default="0")
    error_message = Column(Text, nullable=True)

    url = Column(Text, nullable=True)
    thumbnail_url = Column(Text, nullable=True)
    storage_key = Column(Text, nullable=True)
    mime_type = Column(String, nullable=True)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)

    #: Public URLs of reference images the prompt was seeded with. Stored as
    #: URLs, not bytes: the worker re-fetches them, and the row stays small.
    reference_urls = Column(JSONB, nullable=False, server_default="[]")

    # Set when the user cancels; the worker checks it between polls.
    cancel_requested = Column(Boolean, nullable=False, server_default="false")
    # Generations are reviewed before they join the media panel: a finished row
    # is previewed, then saved or discarded. Only saved rows are project media.
    saved = Column(Boolean, nullable=False, server_default="false", index=True)

    # Provenance, when this was generated by the AI creative director rather
    # than by hand. The compiler joins assets back to the directive that asked
    # for them through `directive_id`, and reverting a director run is a filter
    # over `plan_id` — which is why the link lives here rather than as a tag on
    # the timeline item, where the editor's allow-list loaders would strip it.
    director_plan_id = Column(
        Integer, ForeignKey("director_plans.id", ondelete="SET NULL"), nullable=True, index=True
    )
    director_directive_id = Column(String, nullable=True)

    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)


class DirectorPlan(Base):
    """One run of the AI creative director.

    The row is the run's state machine. Planning, generating the assets it asked
    for, and compiling them onto the timeline are separate stages that can each
    fail or be cancelled independently, and a run that dies mid-way has to be
    resumable rather than restarted — regenerating a dozen images because the
    compile step crashed would be both slow and expensive.

    `plan` holds the validated EditPlan; `applied_manifest` records every id the
    compiler created, which is what makes a revert a precise filter rather than
    a guess (docs/ai_creative_director.md §9.2).
    """

    __tablename__ = "director_plans"

    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(
        Integer, ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id = Column(
        Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    # queued | planning | generating | compiling | ready | applied | failed | cancelled
    status = Column(String, nullable=False, server_default="queued", index=True)
    #: Human-readable stage, surfaced verbatim in the UI. Progress is reported as
    #: a sentence rather than a percentage because the stages are not comparable
    #: in duration — planning is seconds, a Veo shot is minutes.
    stage = Column(String, nullable=True)
    progress = Column(Integer, nullable=False, server_default="0")

    tier = Column(String, nullable=False, server_default="standard")
    brief = Column(Text, nullable=True)
    allow_video = Column(Boolean, nullable=False, server_default="true")

    plan = Column(JSONB, nullable=True)
    usage = Column(JSONB, nullable=True)
    model = Column(String, nullable=True)
    warnings = Column(JSONB, nullable=True)
    applied_manifest = Column(JSONB, nullable=True)

    error_message = Column(Text, nullable=True)
    cancel_requested = Column(Boolean, nullable=False, server_default="false")

    applied_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)


class RoughCutDraft(Base):
    """The canonical rough-cut draft — one revisioned row per project.

    Replaces the untyped `ai_results` row (`result_type="rough_cut_draft"`) as
    the source of truth. That row had no revision, no checksum, no unique
    constraint, and five uncoordinated writers doing whole-document replaces
    (docs/editing-harness-implementation-plan.md §5.2 G1). This table pins the
    draft to the *project* — the workspace was already per-project in practice,
    but ownership was resolved through "newest source video by updated_at",
    which could silently move the draft to a different row mid-session.

    Every write goes through `app.services.draft_store`, which enforces
    optimistic concurrency (`expected_revision`) under a row lock and mirrors
    the payload back into the legacy `ai_results` row so existing readers keep
    working during the migration window.
    """

    __tablename__ = "rough_cut_drafts"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(
        Integer,
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    #: The canonical source video the editor addresses this draft through. Set
    #: explicitly on save — never inferred from `Video.updated_at` ordering.
    video_id = Column(
        Integer, ForeignKey("videos.id", ondelete="SET NULL"), nullable=True, index=True
    )

    revision = Column(Integer, nullable=False, default=0, server_default="0")
    #: sha256 of the canonical JSON serialization of `payload`.
    checksum = Column(String, nullable=True)
    payload = Column(JSONB, nullable=False, server_default="{}")

    #: When a human last wrote through the editor path. Replaces the fragile
    #: `"rangeEditVersion" in payload` heuristic as the do-not-clobber signal.
    user_edited_at = Column(TIMESTAMP, nullable=True)
    #: Which writer produced the current revision: "editor", "auto_edit",
    #: "effect_job", "director", "harness:<run_id>", "seed", "migration".
    last_writer = Column(String, nullable=True)

    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)


class RoughCutDraftRevision(Base):
    """Durable history: one compressed snapshot per draft revision.

    Full snapshots rather than deltas — correctness and recovery first; deltas
    only if storage pressure is ever measured. `snapshot_zlib` is
    zlib-compressed canonical JSON. Retention pruning keeps the recent window
    plus anything a harness run's manifest still references.
    """

    __tablename__ = "rough_cut_draft_revisions"
    __table_args__ = (
        UniqueConstraint("draft_id", "revision", name="uq_rough_cut_draft_revision"),
    )

    id = Column(Integer, primary_key=True, index=True)
    draft_id = Column(
        Integer,
        ForeignKey("rough_cut_drafts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    revision = Column(Integer, nullable=False)
    parent_revision = Column(Integer, nullable=True)
    checksum = Column(String, nullable=True)
    snapshot_zlib = Column(LargeBinary, nullable=True)

    writer = Column(String, nullable=True)
    #: Free-form provenance id: a harness run id, an RQ job id, a request id.
    source_id = Column(String, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)


class HarnessRun(Base):
    """One run of the editing harness: intent → plan → approve → apply → verify.

    The Director's `director_plans` proved the shape (pure compiler behind a
    thin applier, manifest-based revert, reconcile-on-poll); this generalizes
    it beyond additive edits. Two deliberate differences from the Director:
    runs are addressed by id — never by "latest" — and `inverse_manifest`
    survives revert as an audit trail instead of being nulled.
    """

    __tablename__ = "editing_harness_runs"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(
        Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    video_id = Column(
        Integer, ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    workspace_id = Column(
        Integer, ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    # draft | planning | needs_input | planned | approved | staging | applying |
    # verifying | ready | partially_applied | failed | cancelled | conflicted |
    # reverted | superseded
    state = Column(String, nullable=False, default="draft", server_default="draft", index=True)
    #: Human-readable stage sentence, surfaced verbatim (the Director pattern).
    stage = Column(String, nullable=True)

    intent = Column(Text, nullable=True)
    recipe_id = Column(String, nullable=True, index=True)
    recipe_version = Column(Integer, nullable=False, default=1, server_default="1")
    params = Column(JSONB, nullable=True)

    base_draft_revision = Column(Integer, nullable=True)
    applied_draft_revision = Column(Integer, nullable=True)
    base_checksum = Column(String, nullable=True)
    result_checksum = Column(String, nullable=True)

    capability_snapshot = Column(JSONB, nullable=True)
    selection_snapshot = Column(JSONB, nullable=True)
    plan = Column(JSONB, nullable=True)
    plan_checksum = Column(String, nullable=True)
    diff = Column(JSONB, nullable=True)
    estimates = Column(JSONB, nullable=True)

    applied_manifest = Column(JSONB, nullable=True)
    #: Inverse entries recorded at commit; retained after revert (audit trail).
    inverse_manifest = Column(JSONB, nullable=True)
    verification_report = Column(JSONB, nullable=True)
    warnings = Column(JSONB, nullable=True)

    model_provider = Column(String, nullable=True)
    model_name = Column(String, nullable=True)
    prompt_version = Column(String, nullable=True)
    token_usage = Column(JSONB, nullable=True)
    cost_usd = Column(Float, nullable=True)

    error_code = Column(String, nullable=True)
    error_detail = Column(Text, nullable=True)
    request_id = Column(String, nullable=True)
    cancel_requested = Column(Boolean, nullable=False, default=False, server_default="false")

    planned_at = Column(TIMESTAMP, nullable=True)
    approved_at = Column(TIMESTAMP, nullable=True)
    applied_at = Column(TIMESTAMP, nullable=True)
    verified_at = Column(TIMESTAMP, nullable=True)
    reverted_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)


class HarnessOperation(Base):
    """One primitive operation inside a harness run.

    Entity ids are derived from `(run_id, operation_key)` — the Director's
    derived-id idempotency, generalized — so re-applying a run is a structural
    no-op and staging jobs can be deduplicated by deterministic job id.
    """

    __tablename__ = "editing_harness_operations"
    __table_args__ = (
        UniqueConstraint("run_id", "operation_key", name="uq_harness_operation_key"),
        UniqueConstraint("idempotency_key", name="uq_harness_operation_idem"),
    )

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(
        Integer,
        ForeignKey("editing_harness_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    operation_key = Column(String, nullable=False)
    type = Column(String, nullable=False, index=True)
    schema_version = Column(Integer, nullable=False, server_default="1")

    sequence = Column(Integer, nullable=False, default=0, server_default="0")
    depends_on = Column(JSONB, nullable=True)

    # pending | disabled | staging | staged | applying | applied | skipped |
    # failed | cancelled | rolled_back
    state = Column(String, nullable=False, default="pending", server_default="pending", index=True)
    risk = Column(String, nullable=False, default="reversible", server_default="reversible")
    approval_group = Column(String, nullable=True)

    target = Column(JSONB, nullable=True)
    preconditions = Column(JSONB, nullable=True)
    params = Column(JSONB, nullable=True)
    evidence = Column(JSONB, nullable=True)
    confidence = Column(Float, nullable=True)

    result = Column(JSONB, nullable=True)
    #: Staged asset produced by Phase A, e.g. an effect row id + output URL.
    staged_asset = Column(JSONB, nullable=True)
    rollback = Column(JSONB, nullable=True)

    job_id = Column(String, nullable=True)
    idempotency_key = Column(String, nullable=True)
    attempt_count = Column(Integer, nullable=False, server_default="0")

    error_code = Column(String, nullable=True)
    error_detail = Column(Text, nullable=True)

    started_at = Column(TIMESTAMP, nullable=True)
    completed_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)


class UgcProduct(Base):
    """A product/app scraped from a URL, normalized for ad generation."""

    __tablename__ = "ugc_products"
    __table_args__ = {"schema": "aiugc"}

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    workspace_id = Column(
        Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_url = Column(Text, nullable=False)
    source_type = Column(String, nullable=False, server_default="landing")  # shopify|app_store|play|landing
    name = Column(String, nullable=True)
    brand = Column(String, nullable=True)
    price = Column(String, nullable=True)
    currency = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    benefits = Column(JSONB, nullable=False, server_default="[]")
    pain_points = Column(JSONB, nullable=False, server_default="[]")
    use_cases = Column(JSONB, nullable=False, server_default="[]")
    target_audience = Column(JSONB, nullable=True)
    reviews = Column(JSONB, nullable=False, server_default="[]")
    image_urls = Column(JSONB, nullable=False, server_default="[]")
    raw_scrape = Column(JSONB, nullable=True)
    status = Column(String, nullable=False, server_default="pending", index=True)  # pending|ready|failed
    error_message = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    briefs = relationship("UgcBrief", back_populates="product", cascade="all, delete-orphan")
    campaigns = relationship("UgcCampaign", back_populates="product")


class UgcBrief(Base):
    """AI brief + generated hooks/scripts/CTAs derived from a product."""

    __tablename__ = "ugc_briefs"
    __table_args__ = {"schema": "aiugc"}

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(
        Integer, ForeignKey("aiugc.ugc_products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    audience = Column(Text, nullable=True)
    main_promise = Column(Text, nullable=True)
    pain_points = Column(JSONB, nullable=False, server_default="[]")
    objections = Column(JSONB, nullable=False, server_default="[]")
    benefits = Column(JSONB, nullable=False, server_default="[]")
    angles = Column(JSONB, nullable=False, server_default="[]")
    hooks = Column(JSONB, nullable=False, server_default="[]")
    scripts = Column(JSONB, nullable=False, server_default="[]")
    ctas = Column(JSONB, nullable=False, server_default="[]")
    model_meta = Column(JSONB, nullable=True)
    status = Column(String, nullable=False, server_default="pending", index=True)  # pending|ready|failed
    error_message = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    product = relationship("UgcProduct", back_populates="briefs")


class UgcAvatar(Base):
    """Creator/persona catalog entry (platform-curated or provider-synced)."""

    __tablename__ = "ugc_avatars"
    __table_args__ = {"schema": "aiugc"}

    id = Column(Integer, primary_key=True, index=True)
    provider = Column(String, nullable=False, server_default="stub", index=True)
    provider_avatar_id = Column(String, nullable=False)
    name = Column(String, nullable=False)
    thumbnail_url = Column(String, nullable=True)
    age_range = Column(String, nullable=True)
    gender_presentation = Column(String, nullable=True)
    region = Column(String, nullable=True)
    default_voice_id = Column(String, nullable=True)
    accent = Column(String, nullable=True)
    energy = Column(String, nullable=True)
    is_active = Column(Boolean, nullable=False, server_default="true")
    is_premium = Column(Boolean, nullable=False, server_default="false")
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)


class UgcVoice(Base):
    """Voice catalog entry (platform-curated or provider-synced)."""

    __tablename__ = "ugc_voices"
    __table_args__ = {"schema": "aiugc"}

    id = Column(Integer, primary_key=True, index=True)
    provider = Column(String, nullable=False, server_default="stub", index=True)
    provider_voice_id = Column(String, nullable=False)
    name = Column(String, nullable=False)
    gender = Column(String, nullable=True)
    accent = Column(String, nullable=True)
    language = Column(String, nullable=True, server_default="en")
    preview_url = Column(String, nullable=True)
    is_premium = Column(Boolean, nullable=False, server_default="false")
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)


class UgcCampaign(Base):
    """A generation batch for one product (groups variations)."""

    __tablename__ = "ugc_campaigns"
    __table_args__ = {"schema": "aiugc"}

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    workspace_id = Column(
        Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id = Column(
        Integer, ForeignKey("aiugc.ugc_products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    brief_id = Column(
        Integer, ForeignKey("aiugc.ugc_briefs.id", ondelete="SET NULL"), nullable=True
    )
    name = Column(String, nullable=False, server_default="Untitled campaign")
    platform = Column(String, nullable=False, server_default="tiktok")  # tiktok|reels|shorts|meta
    default_aspect_ratio = Column(String, nullable=False, server_default="9:16")
    default_length_sec = Column(Integer, nullable=False, server_default="30")
    settings = Column(JSONB, nullable=True)
    status = Column(String, nullable=False, server_default="draft", index=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    product = relationship("UgcProduct", back_populates="campaigns")
    variations = relationship(
        "UgcVariation", back_populates="campaign", cascade="all, delete-orphan"
    )


class UgcVariation(Base):
    """One UGC ad. Render lifecycle mirrors ``repurpose.Clip``."""

    __tablename__ = "ugc_variations"
    __table_args__ = {"schema": "aiugc"}

    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(
        Integer, ForeignKey("aiugc.ugc_campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name = Column(String, nullable=False, server_default="UGC ad")
    angle = Column(String, nullable=True)
    hook = Column(Text, nullable=True)
    script = Column(Text, nullable=True)
    cta = Column(String, nullable=True)
    caption_style = Column(JSONB, nullable=True)
    # Provider selection is stored as opaque ids so the pipeline stays
    # provider-agnostic; the catalog tables are only for the picker UI.
    provider = Column(String, nullable=False, server_default="stub")
    provider_avatar_id = Column(String, nullable=True)
    provider_voice_id = Column(String, nullable=True)
    avatar_name = Column(String, nullable=True)
    voice_name = Column(String, nullable=True)
    aspect_ratio = Column(String, nullable=False, server_default="9:16")
    length_sec = Column(Integer, nullable=False, server_default="30")
    music_url = Column(String, nullable=True)
    brand_logo_url = Column(String, nullable=True)
    provider_job_id = Column(String, nullable=True)
    status = Column(String, nullable=False, server_default="draft", index=True)
    render_progress = Column(Integer, nullable=False, server_default="0")
    render_error = Column(Text, nullable=True)
    storage_url = Column(String, nullable=True)
    thumbnail_url = Column(String, nullable=True)
    rq_job_id = Column(String, nullable=True)
    is_ai_generated = Column(Boolean, nullable=False, server_default="true")
    disclosure_applied = Column(Boolean, nullable=False, server_default="false")
    completed_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    campaign = relationship("UgcCampaign", back_populates="variations")


class UgcPerformance(Base):
    """Manual or connected ad metrics for a variation (feeds the learner)."""

    __tablename__ = "ugc_performance"
    __table_args__ = {"schema": "aiugc"}

    id = Column(Integer, primary_key=True, index=True)
    variation_id = Column(
        Integer, ForeignKey("aiugc.ugc_variations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source = Column(String, nullable=False, server_default="manual")  # manual|meta|tiktok
    spend = Column(Float, nullable=True)
    impressions = Column(Integer, nullable=True)
    clicks = Column(Integer, nullable=True)
    ctr = Column(Float, nullable=True)
    conversions = Column(Integer, nullable=True)
    cvr = Column(Float, nullable=True)
    roas = Column(Float, nullable=True)
    notes = Column(Text, nullable=True)
    captured_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)


class UgcCreditLedger(Base):
    """Append-only workspace credit ledger. Balance = sum(delta)."""

    __tablename__ = "ugc_credit_ledger"
    __table_args__ = {"schema": "aiugc"}

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(
        Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    delta = Column(Integer, nullable=False)  # +grant / -debit
    reason = Column(String, nullable=False)  # monthly_grant|reserve|debit|refund|topup|account_credit_transfer
    variation_id = Column(
        Integer, ForeignKey("aiugc.ugc_variations.id", ondelete="SET NULL"), nullable=True
    )
    period = Column(String, nullable=True, index=True)  # YYYY-MM for monthly grant idempotency
    balance_after = Column(Integer, nullable=False, server_default="0")
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)


# ---------------------------------------------------------------------------
# Referrals & account credits
# ---------------------------------------------------------------------------


class ReferralCode(Base):
    """
    One share link per user.

    The code is the whole program's identity: it is what a friend arrives with,
    what a guest pass is drawn against, and what a reward is attributed to. It
    lives in its own row rather than a `users.referral_code` column so the pass
    allowance can be raised for one person (a partner, a launch push) without a
    schema change, and so a compromised code can be revoked without touching the
    account.
    """

    __tablename__ = "referral_codes"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False, index=True
    )
    #: Short, case-insensitive, unambiguous (no O/0/I/1) — it gets read aloud.
    code = Column(String, unique=True, nullable=False, index=True)
    #: Guest passes this code may hand out. Per-user so it can be topped up.
    passes_total = Column(Integer, nullable=False, server_default="3")
    #: Set when the code is retired. Existing referrals keep paying out.
    revoked_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User", back_populates="referral_code", foreign_keys=[user_id])


class Referral(Base):
    """
    One redeemed guest pass: the friend who arrived on someone's link.

    A row is created either when an invite is emailed (no account yet, so
    `invitee_user_id` is null) or when someone signs up on the link directly.
    Link *clicks* are not tracked, so "3/3 left" counts people, not opens.
    `status` is the state machine the settings panel renders:

        invited ──accepted──> signed_up -> trialing -> rewarded
           │                                        \\-> void
           └──> expired  (14 days, pass handed back)

    `rewarded` is reached the moment the friend's subscription goes paid-active,
    because the credits are granted in the same transaction that records it.
    """

    __tablename__ = "referrals"
    __table_args__ = (
        # One referral per referred account, ever. Re-signup cannot mint a
        # second reward for the same person.
        UniqueConstraint("invitee_user_id", name="uq_referrals_invitee_user"),
    )

    id = Column(Integer, primary_key=True, index=True)
    referrer_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    referral_code_id = Column(
        Integer, ForeignKey("referral_codes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Snapshot of the code as typed, so a revoked/reissued code still reads back.
    code = Column(String, nullable=False)
    #: Null while an emailed invite is still outstanding.
    invitee_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    #: The address invited, or a snapshot taken at signup — so the referrer sees
    #: who it was even after a deletion anonymizes the account.
    invitee_email = Column(String, nullable=True)
    #: invited | signed_up | trialing | rewarded | expired | void
    status = Column(String, nullable=False, server_default="signed_up", index=True)
    #: Why a referral was voided (self_referral | refunded | manual).
    void_reason = Column(String, nullable=True)
    #: The extended trial the pass buys, and whether it has been spent.
    pass_trial_days = Column(Integer, nullable=False, server_default="30")
    pass_redeemed_at = Column(TIMESTAMP, nullable=True)
    #: Email-invite bookkeeping. `invite_sends` is capped so an invite is a
    #: favour (one mail, one nudge) rather than a campaign.
    invited_at = Column(TIMESTAMP, nullable=True)
    invite_expires_at = Column(TIMESTAMP, nullable=True)
    invite_last_sent_at = Column(TIMESTAMP, nullable=True)
    invite_sends = Column(Integer, nullable=False, server_default="0")
    #: Null until the invite is accepted / the account is created.
    signed_up_at = Column(TIMESTAMP, nullable=True)
    converted_at = Column(TIMESTAMP, nullable=True)
    rewarded_at = Column(TIMESTAMP, nullable=True)
    #: Stripe invoice that proved cash was collected. Required for precise,
    #: idempotent refund reversal; subscription status alone is not payment.
    reward_source_invoice_id = Column(String, nullable=True, index=True)
    reward_reversed_at = Column(TIMESTAMP, nullable=True)
    reward_dispute_active = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    reward_reinstated_at = Column(TIMESTAMP, nullable=True)
    #: Credits actually granted to the referrer, snapshotted so a later change
    #: to the program's terms does not rewrite history.
    reward_credits = Column(Integer, nullable=True)
    #: Existing-account detection is intentionally hidden from the referrer,
    #: but it must not strand one of their finite guest passes.
    capacity_released_at = Column(TIMESTAMP, nullable=True)
    capacity_release_reason = Column(String, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    referrer = relationship("User", foreign_keys=[referrer_user_id], back_populates="referrals_made")
    invitee = relationship("User", foreign_keys=[invitee_user_id])
    referral_code = relationship("ReferralCode")


class ReferralInviteDelivery(Base):
    """One concrete email delivery attempt, successful or failed."""

    __tablename__ = "referral_invite_deliveries"
    __table_args__ = (
        UniqueConstraint("referral_id", "attempt_number", name="uq_referral_delivery_attempt"),
    )

    id = Column(Integer, primary_key=True, index=True)
    referral_id = Column(
        Integer, ForeignKey("referrals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    referrer_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_number = Column(Integer, nullable=False)
    status = Column(String, nullable=False, server_default="queued", index=True)
    error_code = Column(String, nullable=True)
    retry_count = Column(Integer, nullable=False, server_default="0")
    next_retry_at = Column(TIMESTAMP, nullable=True, index=True)
    last_attempt_at = Column(TIMESTAMP, nullable=True)
    suppression_reason = Column(String, nullable=True)
    sent_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False, index=True)

    referral = relationship("Referral")


class ReferralEmailSuppression(Base):
    """Privacy-preserving bounce/complaint suppression for referral mail."""

    __tablename__ = "referral_email_suppressions"

    id = Column(Integer, primary_key=True, index=True)
    email_hash = Column(String, unique=True, nullable=False, index=True)
    reason = Column(String, nullable=False)
    source = Column(String, nullable=False)
    provider_event_id = Column(String, unique=True, nullable=True, index=True)
    suppressed_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    expires_at = Column(TIMESTAMP, nullable=True)
    cleared_at = Column(TIMESTAMP, nullable=True)
    cleared_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class ReferralEmailEvent(Base):
    """Idempotent, sanitized provider delivery-event receipt."""

    __tablename__ = "referral_email_events"

    id = Column(Integer, primary_key=True, index=True)
    provider_event_id = Column(String, unique=True, nullable=False, index=True)
    email_hash = Column(String, nullable=False, index=True)
    event_type = Column(String, nullable=False, index=True)
    occurred_at = Column(TIMESTAMP, nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)


class ReferralAdminAuditEvent(Base):
    """Append-only evidence for pass-capacity and suppression administration."""

    __tablename__ = "referral_admin_audit_events"

    id = Column(Integer, primary_key=True, index=True)
    event_type = Column(String, nullable=False, index=True)
    actor_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    subject_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_ref = Column(String, nullable=True, index=True)
    payload = Column(JSONB, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False, index=True)


class AccountCreditLedger(Base):
    """
    Append-only AI credit ledger for a *user account*. Balance = sum(delta).

    Distinct from :class:`UgcCreditLedger`, which meters one workspace's ad-
    variation spend. These are the account-level AI credits referral rewards pay
    into and the wider credit system will spend from; keeping them apart means
    neither program can silently drain the other.
    """

    __tablename__ = "account_credit_ledger"
    __table_args__ = (
        # Idempotency for anything granted in response to an external event
        # (a webhook fires twice; a job is retried).
        UniqueConstraint("user_id", "reason", "source_ref", name="uq_account_credit_source"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    delta = Column(Integer, nullable=False)  # +grant / -debit
    #: referral_reward | referral_signup_bonus | grant | purchase | debit | reversal | workspace_transfer
    reason = Column(String, nullable=False)
    #: Stable identity of what caused this entry, e.g. "referral:42". Part of the
    #: idempotency key, so it must be null only for genuinely one-off entries.
    source_ref = Column(String, nullable=True)
    description = Column(String, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)

    user = relationship("User")


# ---------------------------------------------------------------------------
# Affiliate program (cash commissions)
# ---------------------------------------------------------------------------


class AffiliateProgramTerms(Base):
    """Immutable commercial rules for one published affiliate-program version.

    A partner and every commission entry point at the exact version they were
    governed by. Changing a percentage therefore creates a new row instead of
    silently rewriting the economics of old referrals.
    """

    __tablename__ = "affiliate_program_terms"

    id = Column(Integer, primary_key=True, index=True)
    version = Column(String, unique=True, nullable=False, index=True)
    status = Column(String, nullable=False, server_default="draft", index=True)
    commission_rate_bps = Column(Integer, nullable=False, server_default="3000")
    commission_months = Column(Integer, nullable=False, server_default="12")
    attribution_window_days = Column(Integer, nullable=False, server_default="60")
    payout_minimum_minor = Column(BigInteger, nullable=False, server_default="5000")
    hold_days = Column(Integer, nullable=False, server_default="30")
    currency = Column(String, nullable=False, server_default="usd")
    commission_basis = Column(
        String,
        nullable=False,
        server_default="invoice_amount_paid_excluding_tax",
    )
    legal_text = Column(Text, nullable=False)
    legal_copy_checksum = Column(String, nullable=False)
    effective_at = Column(TIMESTAMP, nullable=True)
    retired_at = Column(TIMESTAMP, nullable=True)
    created_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)


class AffiliateApplication(Base):
    """A reviewable application snapshot; history is never overwritten."""

    __tablename__ = "affiliate_applications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email = Column(String, nullable=False)
    display_name = Column(String, nullable=False)
    business_name = Column(String, nullable=True)
    website_url = Column(String, nullable=True)
    country_code = Column(String, nullable=False)
    audience_description = Column(Text, nullable=False)
    audience_size = Column(Integer, nullable=True)
    promotion_channels = Column(JSONB, nullable=False)
    payout_currency = Column(String, nullable=False, server_default="usd")
    status = Column(String, nullable=False, server_default="pending", index=True)
    applicant_attested_at = Column(TIMESTAMP, nullable=False)
    reviewed_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    review_notes = Column(Text, nullable=True)
    reviewed_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(
        TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user = relationship("User", foreign_keys=[user_id])


class AffiliatePartner(Base):
    """Approved partner identity, commercial overrides, and payout readiness."""

    __tablename__ = "affiliate_partners"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    application_id = Column(
        Integer,
        ForeignKey("affiliate_applications.id", ondelete="RESTRICT"),
        unique=True,
        nullable=False,
    )
    terms_version_id = Column(
        Integer,
        ForeignKey("affiliate_program_terms.id", ondelete="RESTRICT"),
        nullable=False,
    )
    code = Column(String, unique=True, nullable=False, index=True)
    status = Column(String, nullable=False, server_default="pending_terms", index=True)
    custom_commission_rate_bps = Column(Integer, nullable=True)
    custom_commission_months = Column(Integer, nullable=True)
    stripe_connect_account_id = Column(String, unique=True, nullable=True, index=True)
    payouts_enabled = Column(Boolean, nullable=False, server_default="false")
    risk_status = Column(String, nullable=False, server_default="review", index=True)
    hold_reason = Column(Text, nullable=True)
    approved_at = Column(TIMESTAMP, nullable=False)
    suspended_at = Column(TIMESTAMP, nullable=True)
    closed_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(
        TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user = relationship("User", foreign_keys=[user_id])
    application = relationship("AffiliateApplication")
    terms = relationship("AffiliateProgramTerms")


class AffiliateCampaign(Base):
    """Partner-managed tracking link with a stable slug and lifecycle."""

    __tablename__ = "affiliate_campaigns"
    __table_args__ = (
        UniqueConstraint("partner_id", "slug", name="uq_affiliate_campaign_partner_slug"),
    )

    id = Column(Integer, primary_key=True, index=True)
    partner_id = Column(
        Integer,
        ForeignKey("affiliate_partners.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    slug = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)
    destination_path = Column(String, nullable=False, server_default="/")
    status = Column(String, nullable=False, server_default="active", index=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(
        TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    partner = relationship("AffiliatePartner")


class AffiliateTermsAcceptance(Base):
    """Append-only evidence that a partner accepted a specific ruleset."""

    __tablename__ = "affiliate_terms_acceptances"
    __table_args__ = (
        UniqueConstraint(
            "partner_id", "terms_version_id", name="uq_affiliate_terms_acceptance"
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    partner_id = Column(
        Integer,
        ForeignKey("affiliate_partners.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    terms_version_id = Column(
        Integer,
        ForeignKey("affiliate_program_terms.id", ondelete="RESTRICT"),
        nullable=False,
    )
    accepted_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    ip_hash = Column(String, nullable=True)
    user_agent_hash = Column(String, nullable=True)
    accepted_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)


class AffiliateClick(Base):
    """Pseudonymous first-touch evidence; raw IPs and user agents are not kept."""

    __tablename__ = "affiliate_clicks"

    id = Column(Integer, primary_key=True, index=True)
    partner_id = Column(
        Integer,
        ForeignKey("affiliate_partners.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token = Column(String, unique=True, nullable=False, index=True)
    campaign_id = Column(
        Integer,
        ForeignKey("affiliate_campaigns.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    campaign = Column(String, nullable=True, index=True)
    landing_path = Column(String, nullable=True)
    referrer_host = Column(String, nullable=True)
    ip_hash = Column(String, nullable=True)
    user_agent_hash = Column(String, nullable=True)
    risk_flags = Column(JSONB, nullable=True)
    occurred_at = Column(TIMESTAMP, server_default=func.now(), nullable=False, index=True)
    expires_at = Column(TIMESTAMP, nullable=False, index=True)
    privacy_scrubbed_at = Column(TIMESTAMP, nullable=True)

    partner = relationship("AffiliatePartner")
    campaign_record = relationship("AffiliateCampaign")


class AffiliateAttribution(Base):
    """First eligible affiliate touch attached to one customer account."""

    __tablename__ = "affiliate_attributions"

    id = Column(Integer, primary_key=True, index=True)
    partner_id = Column(
        Integer,
        ForeignKey("affiliate_partners.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    click_id = Column(
        Integer, ForeignKey("affiliate_clicks.id", ondelete="RESTRICT"), nullable=False
    )
    invitee_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="RESTRICT"),
        unique=True,
        nullable=False,
        index=True,
    )
    terms_version_id = Column(
        Integer,
        ForeignKey("affiliate_program_terms.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status = Column(String, nullable=False, server_default="active", index=True)
    void_reason = Column(String, nullable=True)
    risk_flags = Column(JSONB, nullable=True)
    attributed_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    first_paid_at = Column(TIMESTAMP, nullable=True)
    commission_ends_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(
        TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    partner = relationship("AffiliatePartner")
    click = relationship("AffiliateClick")
    invitee = relationship("User", foreign_keys=[invitee_user_id])
    terms = relationship("AffiliateProgramTerms")


class AffiliateComplianceProfile(Base):
    """Tax and sanctions clearance required before a payout can be drafted."""

    __tablename__ = "affiliate_compliance_profiles"

    id = Column(Integer, primary_key=True, index=True)
    partner_id = Column(
        Integer,
        ForeignKey("affiliate_partners.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    tax_residency_country = Column(String, nullable=True)
    tax_form_type = Column(String, nullable=True)
    tax_form_reference_hash = Column(String, nullable=True)
    tax_verified_at = Column(TIMESTAMP, nullable=True)
    tax_verified_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    sanctions_status = Column(String, nullable=False, server_default="pending", index=True)
    sanctions_checked_at = Column(TIMESTAMP, nullable=True)
    sanctions_checked_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    withholding_rate_bps = Column(Integer, nullable=False, server_default="0")
    review_note = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(
        TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    partner = relationship("AffiliatePartner")


class AffiliateLaunchApproval(Base):
    """One MFA-backed human approval of an immutable terms version."""

    __tablename__ = "affiliate_launch_approvals"
    __table_args__ = (
        Index(
            "uq_affiliate_launch_approval_active_role",
            "terms_version_id",
            "approval_role",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
            sqlite_where=text("revoked_at IS NULL"),
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    terms_version_id = Column(
        Integer,
        ForeignKey("affiliate_program_terms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    approval_role = Column(String, nullable=False, index=True)
    approved_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    terms_checksum = Column(String, nullable=False)
    note = Column(Text, nullable=False)
    approved_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    revoked_at = Column(TIMESTAMP, nullable=True)
    revoked_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    terms = relationship("AffiliateProgramTerms")


class AffiliateCommissionEntry(Base):
    """Append-only signed commission ledger.

    Positive entries accrue commission. Refunds, disputes, and manual
    corrections append negative entries; the original evidence remains intact.
    """

    __tablename__ = "affiliate_commission_entries"

    id = Column(Integer, primary_key=True, index=True)
    partner_id = Column(
        Integer,
        ForeignKey("affiliate_partners.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    attribution_id = Column(
        Integer,
        ForeignKey("affiliate_attributions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    terms_version_id = Column(
        Integer,
        ForeignKey("affiliate_program_terms.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_key = Column(String, unique=True, nullable=False, index=True)
    source_event_id = Column(String, nullable=True, index=True)
    entry_type = Column(String, nullable=False, index=True)
    stripe_invoice_id = Column(String, nullable=True, index=True)
    stripe_charge_id = Column(String, nullable=True, index=True)
    amount_minor = Column(BigInteger, nullable=False)
    commissionable_minor = Column(BigInteger, nullable=False)
    rate_bps = Column(Integer, nullable=False)
    currency = Column(String, nullable=False, index=True)
    available_at = Column(TIMESTAMP, nullable=False, index=True)
    description = Column(String, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False, index=True)

    partner = relationship("AffiliatePartner")
    attribution = relationship("AffiliateAttribution")
    terms = relationship("AffiliateProgramTerms")


class AffiliateCommissionState(Base):
    """Mutable projection used to prevent overlapping refund/dispute reversal.

    This is not the financial record; the signed ledger above remains append-
    only. The projection records the latest target so two different Stripe
    event families cannot reverse the same invoice twice.
    """

    __tablename__ = "affiliate_commission_states"

    id = Column(Integer, primary_key=True, index=True)
    stripe_invoice_id = Column(String, unique=True, nullable=False, index=True)
    partner_id = Column(
        Integer, ForeignKey("affiliate_partners.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    attribution_id = Column(
        Integer, ForeignKey("affiliate_attributions.id", ondelete="RESTRICT"), nullable=False
    )
    accrual_entry_id = Column(
        Integer,
        ForeignKey("affiliate_commission_entries.id", ondelete="RESTRICT"),
        unique=True,
        nullable=False,
    )
    accrued_minor = Column(BigInteger, nullable=False)
    refund_target_minor = Column(BigInteger, nullable=False, server_default="0")
    refund_target_commissionable_minor = Column(
        BigInteger, nullable=False, server_default="0"
    )
    dispute_active = Column(Boolean, nullable=False, server_default="false")
    projected_minor = Column(BigInteger, nullable=False)
    updated_at = Column(
        TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    accrual_entry = relationship("AffiliateCommissionEntry")


class AffiliatePayout(Base):
    """One reviewed transfer batch to an affiliate partner."""

    __tablename__ = "affiliate_payouts"

    id = Column(Integer, primary_key=True, index=True)
    partner_id = Column(
        Integer,
        ForeignKey("affiliate_partners.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    currency = Column(String, nullable=False, index=True)
    gross_amount_minor = Column(BigInteger, nullable=False)
    withholding_rate_bps = Column(Integer, nullable=False, server_default="0")
    withholding_minor = Column(BigInteger, nullable=False, server_default="0")
    amount_minor = Column(BigInteger, nullable=False)
    threshold_minor = Column(BigInteger, nullable=False)
    status = Column(String, nullable=False, server_default="draft", index=True)
    period_start = Column(TIMESTAMP, nullable=True)
    period_end = Column(TIMESTAMP, nullable=False)
    stripe_transfer_id = Column(String, unique=True, nullable=True, index=True)
    failure_reason = Column(Text, nullable=True)
    created_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at = Column(TIMESTAMP, nullable=True)
    processed_at = Column(TIMESTAMP, nullable=True)
    paid_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at = Column(
        TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    partner = relationship("AffiliatePartner")


class AffiliatePayoutItem(Base):
    __tablename__ = "affiliate_payout_items"
    __table_args__ = (
        UniqueConstraint("commission_entry_id", name="uq_affiliate_payout_entry"),
    )

    id = Column(Integer, primary_key=True, index=True)
    payout_id = Column(
        Integer,
        ForeignKey("affiliate_payouts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    commission_entry_id = Column(
        Integer,
        ForeignKey("affiliate_commission_entries.id", ondelete="RESTRICT"),
        nullable=False,
    )
    amount_minor = Column(BigInteger, nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)

    payout = relationship("AffiliatePayout")
    commission_entry = relationship("AffiliateCommissionEntry")


class AffiliateAuditEvent(Base):
    """Sanitized operational and administrative audit trail."""

    __tablename__ = "affiliate_audit_events"

    id = Column(Integer, primary_key=True, index=True)
    event_type = Column(String, nullable=False, index=True)
    actor_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    partner_id = Column(
        Integer,
        ForeignKey("affiliate_partners.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    subject_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_ref = Column(String, nullable=True, index=True)
    payload = Column(JSONB, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False, index=True)
