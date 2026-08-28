from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel


class ReviewLinkCreate(BaseModel):
    label: Optional[str] = None
    password: Optional[str] = None
    expires_at: Optional[datetime] = None
    allow_download: bool = False
    approval_required_for_download: bool = False
    allow_comments: bool = True
    allow_export: bool = False
    watermark_enabled: bool = True
    watermark_mode: str = "visible_overlay"
    require_email: bool = False
    nda_required: bool = False
    nda_document_id: Optional[int] = None
    geofence_mode: str = "off"  # off | allowlist | blocklist
    geo_allow_countries: Optional[List[str]] = None
    geo_block_countries: Optional[List[str]] = None
    recording_detection_mode: str = "monitor"
    version_group_id: Optional[str] = None
    version_label: Optional[str] = None


class ReviewLinkUpdate(BaseModel):
    label: Optional[str] = None
    password: Optional[str] = None  # empty string to remove
    expires_at: Optional[datetime] = None
    allow_download: Optional[bool] = None
    approval_required_for_download: Optional[bool] = None
    allow_comments: Optional[bool] = None
    allow_export: Optional[bool] = None
    watermark_enabled: Optional[bool] = None
    watermark_mode: Optional[str] = None
    require_email: Optional[bool] = None
    nda_required: Optional[bool] = None
    nda_document_id: Optional[int] = None
    geofence_mode: Optional[str] = None
    geo_allow_countries: Optional[List[str]] = None
    geo_block_countries: Optional[List[str]] = None
    recording_detection_mode: Optional[str] = None
    version_group_id: Optional[str] = None
    version_label: Optional[str] = None
    revoked: Optional[bool] = None
    revocation_reason: Optional[str] = None


class ReviewLinkResponse(BaseModel):
    id: int
    video_id: int
    token: str
    label: Optional[str] = None
    has_password: bool
    expires_at: Optional[datetime] = None
    allow_download: bool
    approval_required_for_download: bool = False
    allow_comments: bool
    allow_export: bool = False
    watermark_enabled: bool
    watermark_mode: str = "visible_overlay"
    require_email: bool
    nda_required: bool = False
    nda_document_id: Optional[int] = None
    geofence_mode: str = "off"
    geo_allow_countries: Optional[List[str]] = None
    geo_block_countries: Optional[List[str]] = None
    recording_detection_mode: str = "monitor"
    version_group_id: Optional[str] = None
    version_label: Optional[str] = None
    revoked_at: Optional[datetime] = None
    revocation_reason: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    view_count: int = 0
    unique_viewers: int = 0
    total_comments: int = 0
    approvals: int = 0

    model_config = {"from_attributes": True}


class ReviewSessionSummary(BaseModel):
    id: int
    guest_name: Optional[str] = None
    guest_email: Optional[str] = None
    guest_avatar_url: Optional[str] = None
    ip_address: Optional[str] = None
    country_code: Optional[str] = None
    total_watch_seconds: int
    max_position: int
    reached_end: bool
    view_count: int
    first_viewed_at: datetime
    last_viewed_at: datetime
    approved_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ReviewHeatmapBucket(BaseModel):
    second: int
    views: int


class ReviewAnalyticsResponse(BaseModel):
    link: ReviewLinkResponse
    sessions: List[ReviewSessionSummary]
    heatmap: List[ReviewHeatmapBucket]
    rewatch_hotspots: List[ReviewHeatmapBucket] = []
    scene_groups: List["ReviewSceneGroup"] = []
    signoff_count: int = 0
    completion_rate: float = 0.0


# ---- Public (no-auth) endpoint payloads ----


class PublicReviewVideo(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    # Signed app URL for review playback; null until a session exists (never raw storage URL).
    file_path: Optional[str] = None
    duration: Optional[int] = None
    thumbnail_url: Optional[str] = None


class PublicReviewScope(BaseModel):
    revisions_included: int
    revisions_used: int
    change_request_fee_cents: int
    currency: str
    deliverables_locked: bool
    deliverables_unlocked: bool


class PublicReviewLinkInfo(BaseModel):
    token: str
    label: Optional[str] = None
    has_password: bool
    requires_email: bool
    allow_download: bool
    approval_required_for_download: bool = False
    allow_comments: bool
    allow_export: bool = False
    watermark_enabled: bool
    watermark_mode: str = "visible_overlay"
    version_group_id: Optional[str] = None
    version_label: Optional[str] = None
    nda_required: bool = False
    nda_document_id: Optional[int] = None
    nda_document_name: Optional[str] = None
    nda_accepted: bool = False
    geofence_mode: str = "off"
    recording_detection_mode: str = "monitor"
    expired: bool
    revoked: bool
    video: Optional[PublicReviewVideo] = None  # null if password required
    scope: Optional[PublicReviewScope] = None
    client_approve_blockers: List[dict] = []
    workspace_branding: Optional[dict] = None


class PublicReviewAuthRequest(BaseModel):
    password: Optional[str] = None
    guest_name: Optional[str] = None
    guest_email: Optional[str] = None
    guest_avatar_url: Optional[str] = None
    fingerprint: str


class PublicReviewAuthResponse(BaseModel):
    ok: bool
    session_id: Optional[int] = None
    video: Optional[PublicReviewVideo] = None
    watermark_text: Optional[str] = None
    forensic_fingerprint: Optional[str] = None
    error: Optional[str] = None


class PublicReviewEventCreate(BaseModel):
    session_id: int
    event_type: str  # play|pause|seek|progress|ended
    position: int
    range_end: Optional[int] = None
    seq: Optional[int] = None
    meta_info: Optional[dict] = None


class PublicReviewCommentCreate(BaseModel):
    session_id: int
    text: str
    timecode: int
    end_timecode: Optional[int] = None
    drawing_data: Optional[Any] = None
    parent_id: Optional[int] = None
    kind: Optional[str] = "comment"  # comment | change_request
    transcript_segment_index: Optional[int] = None
    word_start_index: Optional[int] = None
    word_end_index: Optional[int] = None
    anchor_text: Optional[str] = None


class PublicReviewCommentUser(BaseModel):
    id: Optional[int] = None
    name: str
    email: Optional[str] = None
    avatar_url: Optional[str] = None
    is_guest: bool


class PublicReviewCommentResponse(BaseModel):
    id: int
    video_id: int
    parent_id: Optional[int] = None
    text: str
    timecode: int
    end_timecode: Optional[int] = None
    drawing_data: Optional[Any] = None
    transcript_segment_index: Optional[int] = None
    word_start_index: Optional[int] = None
    word_end_index: Optional[int] = None
    anchor_text: Optional[str] = None
    transcript_anchor_resolved: bool = True
    transcript_anchor_reason: Optional[str] = None
    transcript_anchor_remap_timecode: Optional[int] = None
    is_resolved: bool = False
    kind: str = "comment"
    status: str = "open"
    user: PublicReviewCommentUser
    likes_count: int = 0
    replies_count: int = 0
    created_at: datetime
    updated_at: datetime
    replies: List["PublicReviewCommentResponse"] = []


PublicReviewCommentResponse.model_rebuild()


class PublicReviewCommentDeltaItem(BaseModel):
    id: int
    parent_id: Optional[int] = None
    updated_at: datetime
    kind: str = "comment"
    status: str = "open"


class PublicReviewCommentDeltaResponse(BaseModel):
    items: List[PublicReviewCommentDeltaItem] = []
    server_time: datetime


class PublicReviewApproveRequest(BaseModel):
    session_id: int
    approved: bool = True
    note: Optional[str] = None


class PublicReviewRequestChangesRequest(BaseModel):
    """POST /review/{token}/request-changes — the guest saying "not yet"."""

    session_id: int
    note: Optional[str] = None
    # Turn the note into a change request on the timeline, so it lands in the
    # editor's revision checklist rather than only in a status field.
    create_comment: bool = True


class PublicReviewMagicSendRequest(BaseModel):
    email: str
    guest_name: Optional[str] = None
    fingerprint: Optional[str] = None


class PublicReviewMagicVerifyRequest(BaseModel):
    magic_token: str
    fingerprint: str
    guest_name: Optional[str] = None
    guest_avatar_url: Optional[str] = None


class PublicReviewSignoffRequest(BaseModel):
    session_id: int
    declaration_text: str
    typed_signature: Optional[str] = None
    signature_image_data: Optional[str] = None  # data:image/png;base64,...


class PublicReviewSignoffResponse(BaseModel):
    id: Optional[int] = None
    ok: bool
    signed_at: datetime
    signer_name: Optional[str] = None
    signer_email: Optional[str] = None
    declaration_text: str
    pdf_url: Optional[str] = None


class PublicReviewDraftRequest(BaseModel):
    session_id: int
    text: str
    timecode: int = 0


class PublicReviewDraftResponse(BaseModel):
    text: str
    timecode: int
    updated_at: datetime


class PublicReviewRoomMessageCreate(BaseModel):
    session_id: int
    body: str


class PublicReviewRoomMessageResponse(BaseModel):
    id: int
    session_id: int
    guest_name: Optional[str] = None
    guest_avatar_url: Optional[str] = None
    body: str
    created_at: datetime


class PublicReviewRecordingCreate(BaseModel):
    session_id: int
    consent_snapshot: Optional[dict] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None


class PublicReviewNDAAcceptRequest(BaseModel):
    guest_name: Optional[str] = None
    guest_email: Optional[str] = None
    fingerprint: str


class PublicReviewNDAAcceptResponse(BaseModel):
    ok: bool
    accepted_at: datetime


class PublicReviewRecordingResponse(BaseModel):
    id: int
    session_id: Optional[int] = None
    status: str
    file_url: Optional[str] = None
    mime_type: Optional[str] = None
    bytes_size: Optional[int] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    archived_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None
    retention_days: Optional[int] = None
    created_at: datetime


class PublicReviewRecordingGovernanceUpdate(BaseModel):
    archived: Optional[bool] = None
    deleted: Optional[bool] = None
    retention_days: Optional[int] = None


class ReviewSceneGroup(BaseModel):
    key: str
    label: str
    comment_count: int
    start_timecode: int
    end_timecode: int


ReviewAnalyticsResponse.model_rebuild()
