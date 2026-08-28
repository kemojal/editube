"""Review link routes — tokenised no-signup video review.

Two route groups live here:

* `/projects/{pid}/videos/{vid}/review-links` — authenticated, for the
  video owner to create / list / revoke / inspect analytics.
* `/review/{token}` — public, no auth. Used by clients who received a
  review link. Handles password gate, guest identity, watch events,
  comments, and approvals.
"""

from __future__ import annotations

import io
import secrets
import hashlib
import logging
import os
from collections import defaultdict
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from starlette.responses import Response
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.api.models.review_links import (
    PublicReviewApproveRequest,
    PublicReviewRequestChangesRequest,
    PublicReviewAuthRequest,
    PublicReviewAuthResponse,
    PublicReviewCommentCreate,
    PublicReviewCommentResponse,
    PublicReviewCommentUser,
    PublicReviewDraftRequest,
    PublicReviewDraftResponse,
    PublicReviewCommentDeltaResponse,
    PublicReviewCommentDeltaItem,
    PublicReviewMagicSendRequest,
    PublicReviewMagicVerifyRequest,
    PublicReviewSignoffRequest,
    PublicReviewSignoffResponse,
    PublicReviewEventCreate,
    PublicReviewRoomMessageCreate,
    PublicReviewRoomMessageResponse,
    PublicReviewRecordingCreate,
    PublicReviewRecordingResponse,
    PublicReviewRecordingGovernanceUpdate,
    PublicReviewNDAAcceptRequest,
    PublicReviewNDAAcceptResponse,
    PublicReviewLinkInfo,
    PublicReviewScope,
    PublicReviewVideo,
    ReviewAnalyticsResponse,
    ReviewHeatmapBucket,
    ReviewLinkCreate,
    ReviewLinkResponse,
    ReviewLinkUpdate,
    ReviewSessionSummary,
    ReviewSceneGroup,
)
from app.db.database import get_db
from app.db.models import (
    Comment,
    Invoice,
    Project,
    ProjectRevision,
    ReviewEvent,
    ReviewLink,
    ReviewMagicToken,
    ReviewSignoff,
    ReviewSession,
    ReviewRoomMessage,
    ReviewRecordingSession,
    VideoTranscription,
    NDADocument,
    NDAAcceptance,
    ReviewForensicAsset,
    ReviewWorkflowRun,
    ReviewWorkflowStage,
    ReviewWorkflowTemplate,
    UserSettings,
    User,
    Video,
    ReviewCommentDraft,
)
from app.api.models.review_workflow import (
    AttachWorkflowRunBody,
    ReviewWorkflowRunResponse,
)
from app.services.comment_export import export_comments
from app.services.comment_workflow import (
    COMMENT_KIND_CHANGE_REQUEST,
    COMMENT_KIND_COMMENT,
    advance_workflow_run,
    client_approve_blockers,
    download_blockers,
    notify_user_ids_for_new_run,
    sync_is_resolved_from_status,
)
from app.jobs.queue import (
    enqueue_comment_notification_email_job,
    enqueue_mention_email_job,
    enqueue_review_forensic_package_job,
)
from app.services.mentions import extract_mention_handles, resolve_mentioned_users
from app.services.notification_prefs import wants_comment_emails
from app.services.video_status import (
    DECISION_APPROVED,
    DECISION_CHANGES_REQUESTED,
    STATUS_IN_REVIEW,
    apply_video_status,
    is_approved,
    record_decision,
)
from app.services.notifications import (
    TYPE_APPROVAL,
    TYPE_CHANGES_REQUESTED,
    TYPE_CLIENT_COMMENT,
    TYPE_MENTION,
    TYPE_REVIEW_WORKFLOW,
    NotificationSpec,
    emit_notifications,
)
from app.utils.email import send_review_magic_link_email
from app.utils.security import (
    get_current_user,
    get_password_hash,
    verify_password,
)
from app.utils.cloudinary import upload_file_to_cloudinary
from app.services.review_media import (
    build_review_media_url,
    head_upstream_video,
    proxy_review_media,
    verify_review_media_sig,
)
from app.services.product_analytics import emit, emit_after_commit, emit_once
from app.services.review_comment_groups import build_review_scene_groups
from app.services.review_analytics import (
    bounded_position,
    build_watch_heatmap,
    is_playback_complete,
    new_playback_milestones,
    normalize_progress_range,
)
from app.services.project_access import can_access_project, list_users_for_mentions
from app.services.comment_visibility import COMMENT_VISIBILITY_PUBLIC, is_client_visible
from app.services.workspace_branding_resolve import branding_public_dict
from app.services.security_audit import log_security_audit_event
from app.services.geoip import extract_client_ip, is_country_allowed, resolve_country_code
from app.services.forensic_watermark import build_forensic_fingerprint, upsert_forensic_asset


# =============================================================================
# Auth router — for the video owner
# =============================================================================

auth_router = APIRouter(
    prefix="/projects/{project_id}/videos/{video_id}/review-links",
    tags=["Review Links"],
)

logger = logging.getLogger(__name__)


def _check_video_owner(
    project_id: int, video_id: int, db: Session, current_user: User
) -> tuple[Video, Project]:
    video = (
        db.query(Video)
        .filter(Video.id == video_id, Video.project_id == project_id)
        .first()
    )
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_access_project(db, current_user.id, project):
        raise HTTPException(status_code=403, detail="Not authorized")
    return video, project


def _link_to_response(link: ReviewLink, db: Session) -> dict:
    session_rows = (
        db.query(
            func.count(ReviewSession.id),
            func.coalesce(func.sum(ReviewSession.view_count), 0),
            func.count(ReviewSession.approved_at),
        )
        .filter(ReviewSession.review_link_id == link.id)
        .first()
    )
    unique_viewers = int(session_rows[0] or 0)
    view_count = int(session_rows[1] or 0)
    approvals = int(session_rows[2] or 0)
    total_comments = (
        db.query(func.count(Comment.id))
        .filter(Comment.review_link_id == link.id)
        .scalar()
        or 0
    )
    return {
        "id": link.id,
        "video_id": link.video_id,
        "token": link.token,
        "label": link.label,
        "has_password": bool(link.password_hash),
        "expires_at": link.expires_at,
        "allow_download": link.allow_download,
        "approval_required_for_download": link.approval_required_for_download,
        "allow_comments": link.allow_comments,
        "allow_export": getattr(link, "allow_export", False),
        "watermark_enabled": link.watermark_enabled,
        "watermark_mode": getattr(link, "watermark_mode", "visible_overlay"),
        "require_email": link.require_email,
        "nda_required": getattr(link, "nda_required", False),
        "nda_document_id": getattr(link, "nda_document_id", None),
        "geofence_mode": getattr(link, "geofence_mode", "off"),
        "geo_allow_countries": getattr(link, "geo_allow_countries", None),
        "geo_block_countries": getattr(link, "geo_block_countries", None),
        "recording_detection_mode": getattr(link, "recording_detection_mode", "monitor"),
        "version_group_id": link.version_group_id,
        "version_label": link.version_label,
        "revoked_at": link.revoked_at,
        "revocation_reason": getattr(link, "revocation_reason", None),
        "created_at": link.created_at,
        "updated_at": link.updated_at,
        "view_count": view_count,
        "unique_viewers": unique_viewers,
        "total_comments": int(total_comments),
        "approvals": approvals,
    }


def _hash_magic_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _nda_identity_key(*, fingerprint: str, guest_email: str | None) -> str:
    key = (guest_email or "").strip().lower() or (fingerprint or "").strip()
    return hashlib.sha256(key.encode("utf-8")).hexdigest() if key else ""


def _enforce_geofence_or_403(link: ReviewLink, country_code: str) -> None:
    mode = getattr(link, "geofence_mode", "off")
    allowed = is_country_allowed(
        mode=mode,
        allow_countries=getattr(link, "geo_allow_countries", None),
        block_countries=getattr(link, "geo_block_countries", None),
        country_code=country_code,
    )
    if not allowed:
        raise HTTPException(status_code=403, detail="Access blocked by geofencing policy")


def _project_deliverables_paid(db: Session, project_id: int) -> bool:
    """Freelancer deliverables lock: if project.deliverables_locked is true,
    at least one invoice must be paid before downloads/final files unlock.
    """
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project or not getattr(project, "deliverables_locked", False):
        return True
    paid = (
        db.query(Invoice.id)
        .filter(Invoice.project_id == project_id, Invoice.status == "paid")
        .first()
    )
    return paid is not None


def _is_session_download_unlocked(
    link: ReviewLink,
    session: Optional[ReviewSession],
    db: Optional[Session] = None,
    video: Optional[Video] = None,
) -> bool:
    if not link.allow_download:
        return False
    if db is not None and video is not None:
        if not _project_deliverables_paid(db, video.project_id):
            return False
        blockers = download_blockers(db, link, session)
        if blockers:
            return False
        return True
    if not link.approval_required_for_download:
        return True
    return bool(session and session.approved_at)


@auth_router.post("", response_model=ReviewLinkResponse)
def create_review_link(
    project_id: int,
    video_id: int,
    body: ReviewLinkCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_owner(project_id, video_id, db, current_user)
    token = secrets.token_urlsafe(24)
    link = ReviewLink(
        video_id=video_id,
        created_by=current_user.id,
        token=token,
        label=body.label,
        password_hash=(
            get_password_hash(body.password) if body.password else None
        ),
        expires_at=body.expires_at,
        allow_download=body.allow_download,
        approval_required_for_download=body.approval_required_for_download,
        allow_comments=body.allow_comments,
        allow_export=getattr(body, "allow_export", False),
        watermark_enabled=body.watermark_enabled,
        watermark_mode=getattr(body, "watermark_mode", "visible_overlay"),
        require_email=body.require_email,
        nda_required=getattr(body, "nda_required", False),
        nda_document_id=getattr(body, "nda_document_id", None),
        geofence_mode=getattr(body, "geofence_mode", "off"),
        geo_allow_countries=getattr(body, "geo_allow_countries", None),
        geo_block_countries=getattr(body, "geo_block_countries", None),
        recording_detection_mode=getattr(body, "recording_detection_mode", "monitor"),
        version_group_id=body.version_group_id,
        version_label=body.version_label,
    )
    db.add(link)
    db.flush()
    log_security_audit_event(
        db,
        action="review_link.create",
        resource_type="review_link",
        actor_user_id=current_user.id,
        actor_type="user",
        resource_id=token,
        project_id=project_id,
        video_id=video_id,
        metadata={"nda_required": getattr(body, "nda_required", False), "geofence_mode": getattr(body, "geofence_mode", "off")},
    )
    project = db.query(Project).filter(Project.id == project_id).first()
    emit(
        db,
        "review_link_created",
        user=current_user,
        workspace_id=project.workspace_id if project else None,
        properties={
            "feature_key": "review_link",
            "project_id": project_id,
            "video_id": video_id,
            "review_link_id": link.id,
            "password_protected": bool(body.password),
            "email_required": bool(body.require_email),
            "nda_required": bool(getattr(body, "nda_required", False)),
            "download_allowed": bool(body.allow_download),
            "result": "success",
        },
    )
    db.commit()
    db.refresh(link)
    return _link_to_response(link, db)


@auth_router.get("", response_model=List[ReviewLinkResponse])
def list_review_links(
    project_id: int,
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_owner(project_id, video_id, db, current_user)
    links = (
        db.query(ReviewLink)
        .filter(ReviewLink.video_id == video_id)
        .order_by(ReviewLink.created_at.desc())
        .all()
    )
    return [_link_to_response(l, db) for l in links]


@auth_router.patch("/{link_id}", response_model=ReviewLinkResponse)
def update_review_link(
    project_id: int,
    video_id: int,
    link_id: int,
    body: ReviewLinkUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_owner(project_id, video_id, db, current_user)
    link = (
        db.query(ReviewLink)
        .filter(ReviewLink.id == link_id, ReviewLink.video_id == video_id)
        .first()
    )
    if not link:
        raise HTTPException(status_code=404, detail="Review link not found")

    data = body.dict(exclude_unset=True)
    revoked_change: bool | None = None
    if "password" in data:
        pw = data.pop("password")
        link.password_hash = get_password_hash(pw) if pw else None
    if "revoked" in data:
        revoked = data.pop("revoked")
        revoked_change = bool(revoked)
        link.revoked_at = datetime.now(timezone.utc) if revoked else None
        if revoked and not data.get("revocation_reason"):
            data["revocation_reason"] = "manual"
    for k, v in data.items():
        setattr(link, k, v)
    log_security_audit_event(
        db,
        action="review_link.update",
        resource_type="review_link",
        actor_user_id=current_user.id,
        actor_type="user",
        resource_id=str(link.id),
        project_id=project_id,
        video_id=video_id,
        review_link_id=link.id,
        metadata={"changed_fields": sorted(list(data.keys()))},
    )
    if revoked_change is True:
        project = db.query(Project).filter(Project.id == project_id).first()
        emit(
            db,
            "review_link_revoked",
            user=current_user,
            workspace_id=project.workspace_id if project else None,
            properties={
                "feature_key": "review_link",
                "project_id": project_id,
                "video_id": video_id,
                "review_link_id": link.id,
                "revocation_reason": link.revocation_reason or "manual",
                "result": "success",
            },
        )
    db.commit()
    db.refresh(link)
    return _link_to_response(link, db)


@auth_router.delete("/{link_id}")
def delete_review_link(
    project_id: int,
    video_id: int,
    link_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_owner(project_id, video_id, db, current_user)
    link = (
        db.query(ReviewLink)
        .filter(ReviewLink.id == link_id, ReviewLink.video_id == video_id)
        .first()
    )
    if not link:
        raise HTTPException(status_code=404, detail="Review link not found")
    log_security_audit_event(
        db,
        action="review_link.delete",
        resource_type="review_link",
        actor_user_id=current_user.id,
        actor_type="user",
        resource_id=str(link.id),
        project_id=project_id,
        video_id=video_id,
        review_link_id=link.id,
    )
    project = db.query(Project).filter(Project.id == project_id).first()
    emit(
        db,
        "review_link_revoked",
        user=current_user,
        workspace_id=project.workspace_id if project else None,
        properties={
            "feature_key": "review_link",
            "project_id": project_id,
            "video_id": video_id,
            "review_link_id": link.id,
            "revocation_reason": "deleted",
            "result": "success",
        },
    )
    db.delete(link)
    db.commit()
    return {"ok": True}


@auth_router.get("/{link_id}/analytics", response_model=ReviewAnalyticsResponse)
def link_analytics(
    project_id: int,
    video_id: int,
    link_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_owner(project_id, video_id, db, current_user)
    link = (
        db.query(ReviewLink)
        .filter(ReviewLink.id == link_id, ReviewLink.video_id == video_id)
        .first()
    )
    if not link:
        raise HTTPException(status_code=404, detail="Review link not found")

    sessions = (
        db.query(ReviewSession)
        .filter(ReviewSession.review_link_id == link_id)
        .order_by(ReviewSession.last_viewed_at.desc())
        .all()
    )
    video = db.query(Video).filter(Video.id == link.video_id).first()
    session_ids = [s.id for s in sessions]
    progress_events: list[ReviewEvent] = []
    if session_ids:
        progress_events = (
            db.query(ReviewEvent)
            .filter(
                ReviewEvent.session_id.in_(session_ids),
                ReviewEvent.event_type == "progress",
            )
            .all()
        )
    watch_map = build_watch_heatmap(
        progress_events,
        video.duration if video else None,
    )
    heatmap = [
        ReviewHeatmapBucket(second=second, views=views)
        for second, views in watch_map.unique_views.items()
    ]
    rewatch_hotspots = [
        ReviewHeatmapBucket(second=second, views=views)
        for second, views in sorted(
            watch_map.replay_views.items(), key=lambda item: (-item[1], item[0])
        )[:20]
    ]
    signoff_count = (
        db.query(func.count(ReviewSignoff.id))
        .filter(ReviewSignoff.review_link_id == link_id)
        .scalar()
        or 0
    )
    completed_sessions = sum(1 for s in sessions if s.reached_end)
    completion_rate = (completed_sessions / len(sessions)) if sessions else 0.0
    comments = (
        db.query(Comment)
        .filter(Comment.review_link_id == link.id, Comment.parent_id.is_(None))
        .order_by(Comment.timecode.asc())
        .all()
    )
    scene_groups = build_review_scene_groups(db, link.video_id, comments)
    return ReviewAnalyticsResponse(
        link=_link_to_response(link, db),
        sessions=sessions,
        heatmap=heatmap,
        rewatch_hotspots=rewatch_hotspots,
        scene_groups=scene_groups,
        signoff_count=int(signoff_count),
        completion_rate=completion_rate,
    )


@auth_router.get("/{link_id}/recordings", response_model=List[PublicReviewRecordingResponse])
def owner_list_recordings(
    project_id: int,
    video_id: int,
    link_id: int,
    include_deleted: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_owner(project_id, video_id, db, current_user)
    q = db.query(ReviewRecordingSession).filter(ReviewRecordingSession.review_link_id == link_id)
    if not include_deleted:
        q = q.filter(ReviewRecordingSession.deleted_at.is_(None))
    rows = q.order_by(ReviewRecordingSession.created_at.desc()).all()
    return [
        PublicReviewRecordingResponse(
            id=row.id,
            session_id=row.session_id,
            status=row.status,
            file_url=row.file_url,
            mime_type=row.mime_type,
            bytes_size=row.bytes_size,
            started_at=row.started_at,
            ended_at=row.ended_at,
            archived_at=row.archived_at,
            deleted_at=row.deleted_at,
            retention_days=row.retention_days,
            created_at=row.created_at,
        )
        for row in rows
    ]


@auth_router.patch(
    "/{link_id}/recordings/{recording_id}",
    response_model=PublicReviewRecordingResponse,
)
def owner_update_recording_governance(
    project_id: int,
    video_id: int,
    link_id: int,
    recording_id: int,
    body: PublicReviewRecordingGovernanceUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_owner(project_id, video_id, db, current_user)
    row = (
        db.query(ReviewRecordingSession)
        .filter(
            ReviewRecordingSession.id == recording_id,
            ReviewRecordingSession.review_link_id == link_id,
        )
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Recording not found")
    now = datetime.now(timezone.utc)
    if body.archived is not None:
        row.archived_at = now if body.archived else None
    if body.deleted is not None:
        row.deleted_at = now if body.deleted else None
    if body.retention_days is not None:
        row.retention_days = max(0, int(body.retention_days))
    db.commit()
    db.refresh(row)
    return PublicReviewRecordingResponse(
        id=row.id,
        session_id=row.session_id,
        status=row.status,
        file_url=row.file_url,
        mime_type=row.mime_type,
        bytes_size=row.bytes_size,
        started_at=row.started_at,
        ended_at=row.ended_at,
        archived_at=row.archived_at,
        deleted_at=row.deleted_at,
        retention_days=row.retention_days,
        created_at=row.created_at,
    )


@auth_router.delete("/{link_id}/recordings/{recording_id}")
def owner_purge_recording(
    project_id: int,
    video_id: int,
    link_id: int,
    recording_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_owner(project_id, video_id, db, current_user)
    row = (
        db.query(ReviewRecordingSession)
        .filter(
            ReviewRecordingSession.id == recording_id,
            ReviewRecordingSession.review_link_id == link_id,
        )
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Recording not found")
    if row.deleted_at is None:
        raise HTTPException(
            status_code=400,
            detail="Recording must be soft-deleted before purge",
        )
    db.delete(row)
    db.commit()
    return {"ok": True}


@auth_router.post("/{link_id}/send-invite")
def send_owner_invite(
    project_id: int,
    video_id: int,
    link_id: int,
    body: PublicReviewMagicSendRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_owner(project_id, video_id, db, current_user)
    link = (
        db.query(ReviewLink)
        .filter(ReviewLink.id == link_id, ReviewLink.video_id == video_id)
        .first()
    )
    if not link:
        raise HTTPException(status_code=404, detail="Review link not found")
    _assert_link_usable(link)

    # Owner invite path always uses email-gated auth.
    if not link.require_email:
        link.require_email = True
        db.commit()

    recent_count = (
        db.query(func.count(ReviewMagicToken.id))
        .filter(
            ReviewMagicToken.review_link_id == link.id,
            ReviewMagicToken.email == body.email.lower().strip(),
            ReviewMagicToken.created_at
            >= datetime.now(timezone.utc) - timedelta(minutes=10),
        )
        .scalar()
        or 0
    )
    if recent_count >= 5:
        raise HTTPException(status_code=429, detail="Too many invite emails sent")

    raw_token = secrets.token_urlsafe(32)
    frontend_base = str(request.base_url).rstrip("/").replace("/api", "", 1)
    verify_url = f"{frontend_base}/review/{link.token}?magic_token={raw_token}"
    rec = ReviewMagicToken(
        review_link_id=link.id,
        email=body.email.lower().strip(),
        guest_name=(body.guest_name or "").strip() or None,
        token_hash=_hash_magic_token(raw_token),
        fingerprint=body.fingerprint,
        ip_address=request.client.host if request.client else None,
        invited_by_user_id=current_user.id,
        source="owner_invite",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=20),
    )
    db.add(rec)
    project = db.query(Project).filter(Project.id == project_id).first()
    emit(
        db,
        "review_link_invite_sent",
        user=current_user,
        workspace_id=project.workspace_id if project else None,
        properties={
            "feature_key": "review_link",
            "project_id": project_id,
            "video_id": video_id,
            "review_link_id": link.id,
            "invite_method": "email",
            "result": "success",
        },
    )
    db.commit()
    sent = send_review_magic_link_email(
        to_email=rec.email,
        review_label=link.label or "Video review",
        verify_url=verify_url,
        expires_minutes=20,
        recipient_name=rec.guest_name,
        inviter_name=current_user.name if current_user else None,
    )
    return {"ok": bool(sent), "require_email": True}


async def _notify_review_workflow_users(
    db: Session,
    user_ids: list[int],
    project_id: int,
    video_id: int,
) -> None:
    await emit_notifications(
        db,
        [
            NotificationSpec(
                user_id=uid,
                type=TYPE_REVIEW_WORKFLOW,
                project_id=project_id,
                video_id=video_id,
                message="A review stage needs your attention",
            )
            for uid in set(user_ids)
        ],
    )


def _workflow_run_api_response(db: Session, run: ReviewWorkflowRun) -> dict:
    total = (
        db.query(func.count(ReviewWorkflowStage.id))
        .filter(ReviewWorkflowStage.template_id == run.template_id)
        .scalar()
        or 0
    )
    return {
        "id": run.id,
        "review_link_id": run.review_link_id,
        "template_id": run.template_id,
        "current_stage_index": run.current_stage_index,
        "completed_at": run.completed_at,
        "total_stages": int(total),
    }


@auth_router.post("/{link_id}/workflow-run", response_model=ReviewWorkflowRunResponse)
async def attach_workflow_run(
    project_id: int,
    video_id: int,
    link_id: int,
    body: AttachWorkflowRunBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_owner(project_id, video_id, db, current_user)
    link = (
        db.query(ReviewLink)
        .filter(ReviewLink.id == link_id, ReviewLink.video_id == video_id)
        .first()
    )
    if not link:
        raise HTTPException(status_code=404, detail="Review link not found")
    tpl = (
        db.query(ReviewWorkflowTemplate)
        .filter(
            ReviewWorkflowTemplate.id == body.template_id,
            ReviewWorkflowTemplate.project_id == project_id,
        )
        .first()
    )
    if not tpl:
        raise HTTPException(status_code=404, detail="Workflow template not found")
    existing = (
        db.query(ReviewWorkflowRun).filter(ReviewWorkflowRun.review_link_id == link.id).first()
    )
    if existing:
        db.delete(existing)
        db.flush()
    run = ReviewWorkflowRun(
        review_link_id=link.id,
        template_id=tpl.id,
        current_stage_index=0,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    targets = notify_user_ids_for_new_run(db, run)
    await _notify_review_workflow_users(db, targets, project_id, video_id)
    return _workflow_run_api_response(db, run)


@auth_router.post("/{link_id}/workflow-run/advance", response_model=ReviewWorkflowRunResponse)
async def advance_review_workflow(
    project_id: int,
    video_id: int,
    link_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_owner(project_id, video_id, db, current_user)
    link = (
        db.query(ReviewLink)
        .filter(ReviewLink.id == link_id, ReviewLink.video_id == video_id)
        .first()
    )
    if not link:
        raise HTTPException(status_code=404, detail="Review link not found")
    run = db.query(ReviewWorkflowRun).filter(ReviewWorkflowRun.review_link_id == link.id).first()
    if not run:
        raise HTTPException(status_code=404, detail="No workflow attached to this link")
    targets = advance_workflow_run(db, run)
    db.commit()
    db.refresh(run)
    await _notify_review_workflow_users(db, targets, project_id, video_id)
    return _workflow_run_api_response(db, run)


@auth_router.delete("/{link_id}/workflow-run")
def delete_workflow_run(
    project_id: int,
    video_id: int,
    link_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_owner(project_id, video_id, db, current_user)
    link = (
        db.query(ReviewLink)
        .filter(ReviewLink.id == link_id, ReviewLink.video_id == video_id)
        .first()
    )
    if not link:
        raise HTTPException(status_code=404, detail="Review link not found")
    run = db.query(ReviewWorkflowRun).filter(ReviewWorkflowRun.review_link_id == link.id).first()
    if run:
        db.delete(run)
        db.commit()
    return {"ok": True}


# =============================================================================
# Public router — no auth, token-based
# =============================================================================

public_router = APIRouter(prefix="/review", tags=["Review (public)"])


def _get_link_or_404(token: str, db: Session) -> ReviewLink:
    link = db.query(ReviewLink).filter(ReviewLink.token == token).first()
    if not link:
        raise HTTPException(status_code=404, detail="Review link not found")
    return link


def _link_expired(link: ReviewLink) -> bool:
    if link.expires_at is None:
        return False
    now = datetime.now(timezone.utc)
    exp = link.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp < now


def _api_base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _public_video_meta(video: Video) -> PublicReviewVideo:
    return PublicReviewVideo(
        id=video.id,
        name=video.name or "",
        description=video.description,
        file_path=None,
        duration=video.duration,
        thumbnail_url=video.thumbnail_url,
    )


def _public_video_streaming(
    video: Video,
    request: Request,
    token: str,
    session_id: int,
) -> PublicReviewVideo:
    playback_url = build_review_media_url(
        api_base=_api_base(request),
        token=token,
        session_id=session_id,
        purpose="playback",
    )
    return PublicReviewVideo(
        id=video.id,
        name=video.name or "",
        description=video.description,
        file_path=playback_url,
        duration=video.duration,
        thumbnail_url=video.thumbnail_url,
    )


def _assert_link_usable(link: ReviewLink) -> None:
    if link.revoked_at is not None:
        raise HTTPException(status_code=410, detail="Review link has been revoked")
    if _link_expired(link):
        raise HTTPException(status_code=410, detail="Review link has expired")


@public_router.api_route("/{token}/media", methods=["GET", "HEAD"])
async def review_media_proxy(
    token: str,
    request: Request,
    session_id: int,
    purpose: str,
    exp: int,
    sig: str,
    db: Session = Depends(get_db),
):
    if purpose not in ("playback", "download"):
        raise HTTPException(status_code=400, detail="Invalid purpose")
    link = _get_link_or_404(token, db)
    _assert_link_usable(link)
    if not verify_review_media_sig(token, session_id, purpose, exp, sig):
        raise HTTPException(status_code=403, detail="Invalid signature")
    now_ts = int(datetime.now(timezone.utc).timestamp())
    if exp < now_ts:
        raise HTTPException(status_code=403, detail="Signature expired")
    session = _get_session_or_404(link, session_id, db)
    video = db.query(Video).filter(Video.id == link.video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if purpose == "download":
        if not _is_session_download_unlocked(link, session, db=db, video=video):
            raise HTTPException(status_code=403, detail="Download not allowed")
    fp = (video.file_path or "").strip()
    if request.method == "HEAD" and (
        fp.startswith("http://") or fp.startswith("https://")
    ):
        status, headers = await head_upstream_video(fp)
        return Response(status_code=status, headers=headers)
    response = await proxy_review_media(request=request, video=video, purpose=purpose, db=db)
    if purpose == "download" and request.method == "GET":
        project = db.query(Project).filter(Project.id == video.project_id).first()
        from starlette.background import BackgroundTask

        response.background = BackgroundTask(
            emit_after_commit,
            "review_download_completed",
            workspace_id=project.workspace_id if project else None,
            anonymous_id=f"review-session:{session.id}",
            properties={
                "feature_key": "delivery",
                "project_id": video.project_id,
                "video_id": video.id,
                "review_link_id": link.id,
                "review_session_id": session.id,
                "completion_type": "media_response_finished",
                "result": "success",
            },
            source="review_service",
        )
    return response


@public_router.get("/{token}/download-url")
def review_download_url(
    token: str,
    session_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    link = _get_link_or_404(token, db)
    _assert_link_usable(link)
    session = _get_session_or_404(link, session_id, db)
    video = db.query(Video).filter(Video.id == link.video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if not _is_session_download_unlocked(link, session, db=db, video=video):
        raise HTTPException(status_code=403, detail="Download not allowed")
    log_security_audit_event(
        db,
        action="review_link.download_url_issued",
        resource_type="review_link",
        resource_id=str(link.id),
        actor_type="guest",
        review_link_id=link.id,
        session_id=session.id,
        video_id=link.video_id,
        ip_address=session.ip_address,
        country_code=getattr(session, "country_code", None),
        user_agent=session.user_agent,
    )
    project = db.query(Project).filter(Project.id == video.project_id).first()
    emit(
        db,
        "review_download_attempted",
        workspace_id=project.workspace_id if project else None,
        anonymous_id=f"review-session:{session.id}",
        properties={
            "feature_key": "delivery",
            "project_id": video.project_id,
            "video_id": video.id,
            "review_link_id": link.id,
            "review_session_id": session.id,
            "result": "allowed",
        },
        source="review_service",
    )
    db.commit()
    url = build_review_media_url(
        api_base=_api_base(request),
        token=token,
        session_id=session_id,
        purpose="download",
    )
    return {"ok": True, "url": url}


@public_router.post("/{token}/guest-avatar")
def upload_guest_avatar(
    token: str,
    session_id: int = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    link = _get_link_or_404(token, db)
    _assert_link_usable(link)
    session = _get_session_or_404(link, session_id, db)
    ct = (file.content_type or "").lower()
    if ct not in ("image/jpeg", "image/jpg", "image/png", "image/webp"):
        raise HTTPException(status_code=400, detail="Use JPEG, PNG, or WebP")
    url = upload_file_to_cloudinary(file, resource_type="image")
    session.guest_avatar_url = url
    db.commit()
    db.refresh(session)
    return {"ok": True, "guest_avatar_url": url}


@public_router.get("/{token}", response_model=PublicReviewLinkInfo)
def get_public_link_info(token: str, request: Request, db: Session = Depends(get_db)):
    link = _get_link_or_404(token, db)
    expired = _link_expired(link)
    revoked = link.revoked_at is not None
    video_payload: Optional[PublicReviewVideo] = None
    scope_payload: Optional[PublicReviewScope] = None
    video = db.query(Video).filter(Video.id == link.video_id).first()
    if not link.password_hash and not expired and not revoked and video:
        video_payload = _public_video_meta(video)
    if video:
        project = db.query(Project).filter(Project.id == video.project_id).first()
        if project:
            scope_payload = PublicReviewScope(
                revisions_included=project.scope_revisions_included or 0,
                revisions_used=project.revision_count or 0,
                change_request_fee_cents=project.change_request_fee_cents or 0,
                currency=project.currency or "USD",
                deliverables_locked=bool(project.deliverables_locked),
                deliverables_unlocked=_project_deliverables_paid(db, project.id),
            )
    blockers: list[dict] = []
    if video_payload:
        blockers = client_approve_blockers(db, link)

    branding = None
    if video:
        proj = db.query(Project).filter(Project.id == video.project_id).first()
        if proj:
            branding = branding_public_dict(db, proj)

    client_ip = extract_client_ip(dict(request.headers), request.client.host if request.client else None)
    country_code = resolve_country_code(client_ip)
    if not expired and not revoked:
        _enforce_geofence_or_403(link, country_code)

    nda_doc = None
    nda_accepted = False
    if getattr(link, "nda_document_id", None):
        nda_doc = db.query(NDADocument).filter(NDADocument.id == link.nda_document_id).first()

    return PublicReviewLinkInfo(
        token=link.token,
        label=link.label,
        has_password=bool(link.password_hash),
        requires_email=link.require_email,
        allow_download=link.allow_download,
        approval_required_for_download=link.approval_required_for_download,
        allow_comments=link.allow_comments,
        allow_export=getattr(link, "allow_export", False),
        watermark_enabled=link.watermark_enabled,
        watermark_mode=getattr(link, "watermark_mode", "visible_overlay"),
        version_group_id=link.version_group_id,
        version_label=link.version_label,
        nda_required=getattr(link, "nda_required", False),
        nda_document_id=getattr(link, "nda_document_id", None),
        nda_document_name=(nda_doc.name if nda_doc else None),
        nda_accepted=nda_accepted,
        geofence_mode=getattr(link, "geofence_mode", "off"),
        recording_detection_mode=getattr(link, "recording_detection_mode", "monitor"),
        expired=expired,
        revoked=revoked,
        video=video_payload,
        scope=scope_payload,
        client_approve_blockers=blockers,
        workspace_branding=branding,
    )


@public_router.post("/{token}/session", response_model=PublicReviewAuthResponse)
def start_session(
    token: str,
    body: PublicReviewAuthRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    link = _get_link_or_404(token, db)
    _assert_link_usable(link)

    if link.password_hash:
        if not body.password or not verify_password(body.password, link.password_hash):
            return PublicReviewAuthResponse(ok=False, error="Incorrect password")

    if link.require_email and not body.guest_email:
        return PublicReviewAuthResponse(ok=False, error="Email required")

    session = (
        db.query(ReviewSession)
        .filter(
            ReviewSession.review_link_id == link.id,
            ReviewSession.fingerprint == body.fingerprint,
        )
        .first()
    )
    now = datetime.now(timezone.utc)
    client_ip = extract_client_ip(dict(request.headers), request.client.host if request.client else None)
    country_code = resolve_country_code(client_ip)
    _enforce_geofence_or_403(link, country_code)
    user_agent = request.headers.get("user-agent")

    if getattr(link, "nda_required", False):
        identity_key = _nda_identity_key(fingerprint=body.fingerprint, guest_email=body.guest_email)
        accepted = (
            db.query(NDAAcceptance)
            .filter(
                NDAAcceptance.review_link_id == link.id,
                NDAAcceptance.identity_key == identity_key,
                NDAAcceptance.nda_document_id == getattr(link, "nda_document_id", None),
            )
            .first()
        )
        if not accepted:
            return PublicReviewAuthResponse(ok=False, error="NDA acceptance required")

    created_new_session = session is None
    if session:
        session.view_count = (session.view_count or 0) + 1
        session.last_viewed_at = now
        if body.guest_name:
            session.guest_name = body.guest_name
        if body.guest_email:
            session.guest_email = body.guest_email
        if body.guest_avatar_url:
            session.guest_avatar_url = body.guest_avatar_url
    else:
        session = ReviewSession(
            review_link_id=link.id,
            fingerprint=body.fingerprint,
            guest_name=body.guest_name,
            guest_email=body.guest_email,
            guest_avatar_url=body.guest_avatar_url,
            ip_address=client_ip,
            country_code=country_code,
            user_agent=user_agent,
            view_count=1,
        )
        db.add(session)

    session.country_code = country_code
    db.commit()
    db.refresh(session)

    video = db.query(Video).filter(Video.id == link.video_id).first()
    watermark = None
    if link.watermark_enabled:
        who = body.guest_name or body.guest_email or (client_ip or "guest")
        watermark = f"{who} • {now.strftime('%Y-%m-%d %H:%M UTC')}"
    session.watermark_payload = {
        "guest_name": body.guest_name,
        "guest_email": body.guest_email,
        "ip_address": client_ip,
        "country_code": country_code,
        "timestamp_utc": now.isoformat(),
    }
    forensic_fp = build_forensic_fingerprint(link, session, country_code)
    forensic_asset = upsert_forensic_asset(
        db,
        link=link,
        session=session,
        fingerprint=forensic_fp,
        playback_manifest_url=None,
    )
    if getattr(link, "watermark_mode", "visible_overlay") == "forensic":
        queued = enqueue_review_forensic_package_job(forensic_asset.id)
        if not queued:
            api_base = _api_base(request)
            forensic_asset.playback_manifest_url = build_review_media_url(
                api_base=api_base,
                token=link.token,
                session_id=session.id,
                purpose="playback",
            )
            forensic_asset.package_status = "ready"
    db.flush()
    log_security_audit_event(
        db,
        action="review_link.session_start",
        resource_type="review_session",
        resource_id=str(session.id),
        actor_type="guest",
        review_link_id=link.id,
        session_id=session.id,
        video_id=link.video_id,
        ip_address=client_ip,
        country_code=country_code,
        user_agent=user_agent,
        metadata={"forensic_asset_id": forensic_asset.id, "recording_detection_mode": getattr(link, "recording_detection_mode", "monitor")},
    )
    project = db.query(Project).filter(Project.id == video.project_id).first() if video else None
    review_properties = {
        "feature_key": "review_link",
        "project_id": video.project_id if video else None,
        "video_id": link.video_id,
        "review_link_id": link.id,
        "review_session_id": session.id,
        "session_is_new": created_new_session,
        "view_count": session.view_count or 1,
        "password_required": bool(link.password_hash),
        "email_required": bool(link.require_email),
        "nda_required": bool(getattr(link, "nda_required", False)),
        "country_code": country_code,
        "result": "success",
    }
    emit(
        db,
        "review_link_opened",
        workspace_id=project.workspace_id if project else None,
        anonymous_id=f"review-session:{session.id}",
        properties=review_properties,
        source="review_service",
    )
    if created_new_session:
        emit(
            db,
            "review_guest_session_started",
            workspace_id=project.workspace_id if project else None,
            anonymous_id=f"review-session:{session.id}",
            properties=review_properties,
            source="review_service",
        )
    db.commit()

    video_payload = _public_video_streaming(video, request, link.token, session.id) if video else None
    if (
        video_payload is not None
        and getattr(link, "watermark_mode", "visible_overlay") == "forensic"
        and forensic_asset.playback_manifest_url
    ):
        video_payload.file_path = forensic_asset.playback_manifest_url

    return PublicReviewAuthResponse(
        ok=True,
        session_id=session.id,
        video=video_payload,
        watermark_text=watermark,
        forensic_fingerprint=forensic_fp,
    )


@public_router.post("/{token}/nda/accept", response_model=PublicReviewNDAAcceptResponse)
def accept_nda(
    token: str,
    body: PublicReviewNDAAcceptRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    link = _get_link_or_404(token, db)
    _assert_link_usable(link)
    if not getattr(link, "nda_required", False):
        raise HTTPException(status_code=400, detail="NDA is not required for this link")
    nda_doc_id = getattr(link, "nda_document_id", None)
    if not nda_doc_id:
        raise HTTPException(status_code=400, detail="NDA document is not configured")
    doc = db.query(NDADocument).filter(NDADocument.id == nda_doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="NDA document not found")

    identity_key = _nda_identity_key(fingerprint=body.fingerprint, guest_email=body.guest_email)
    if not identity_key:
        raise HTTPException(status_code=400, detail="Missing NDA identity")
    client_ip = extract_client_ip(dict(request.headers), request.client.host if request.client else None)
    user_agent = request.headers.get("user-agent")
    row = (
        db.query(NDAAcceptance)
        .filter(
            NDAAcceptance.review_link_id == link.id,
            NDAAcceptance.identity_key == identity_key,
            NDAAcceptance.nda_document_id == nda_doc_id,
        )
        .first()
    )
    if not row:
        row = NDAAcceptance(
            review_link_id=link.id,
            nda_document_id=nda_doc_id,
            identity_key=identity_key,
            guest_name=body.guest_name,
            guest_email=body.guest_email,
            ip_address=client_ip,
            user_agent=user_agent,
        )
        db.add(row)
    else:
        row.guest_name = body.guest_name or row.guest_name
        row.guest_email = body.guest_email or row.guest_email
        row.ip_address = client_ip or row.ip_address
        row.user_agent = user_agent or row.user_agent
    log_security_audit_event(
        db,
        action="review_link.nda_accept",
        resource_type="nda_acceptance",
        resource_id=identity_key,
        actor_type="guest",
        review_link_id=link.id,
        video_id=link.video_id,
        ip_address=client_ip,
        user_agent=user_agent,
        metadata={"nda_document_id": nda_doc_id, "fingerprint": body.fingerprint},
    )
    db.commit()
    db.refresh(row)
    return PublicReviewNDAAcceptResponse(ok=True, accepted_at=row.accepted_at)


def _get_session_or_404(
    link: ReviewLink,
    session_id: int,
    db: Session,
    *,
    for_update: bool = False,
) -> ReviewSession:
    query = db.query(ReviewSession).filter(
        ReviewSession.id == session_id,
        ReviewSession.review_link_id == link.id,
    )
    session = query.with_for_update().first() if for_update else query.first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@public_router.post("/{token}/events")
def record_event(
    token: str,
    body: PublicReviewEventCreate,
    db: Session = Depends(get_db),
):
    link = _get_link_or_404(token, db)
    _assert_link_usable(link)
    session = _get_session_or_404(link, body.session_id, db, for_update=True)
    if body.seq is not None:
        existing_event = (
            db.query(ReviewEvent.id)
            .filter(
                ReviewEvent.session_id == session.id,
                ReviewEvent.seq == body.seq,
            )
            .first()
        )
        if existing_event:
            return {"ok": True, "deduplicated": True}

    video = db.query(Video).filter(Video.id == link.video_id).first()
    duration = video.duration if video else None
    position = bounded_position(body.position, duration)
    progress_range = normalize_progress_range(body.position, body.range_end, duration)

    ev = ReviewEvent(
        session_id=session.id,
        event_type=body.event_type,
        position=position,
        seq=body.seq,
        meta_info=getattr(body, "meta_info", None),
        range_end=progress_range[1] if progress_range else None,
    )
    db.add(ev)

    # Update aggregates cheaply
    session.last_viewed_at = datetime.now(timezone.utc)
    furthest_position = progress_range[1] if progress_range else position
    if furthest_position > (session.max_position or 0):
        session.max_position = furthest_position
    if body.event_type == "progress" and progress_range is not None:
        delta = progress_range[1] - progress_range[0]
        session.total_watch_seconds = (session.total_watch_seconds or 0) + delta
    ended = body.event_type == "ended"
    session.reached_end = bool(session.reached_end) or is_playback_complete(
        session.max_position,
        duration,
        ended=ended,
    )

    prior_milestones = session.analytics_milestones or []
    milestones = new_playback_milestones(
        session.max_position,
        duration,
        ended=ended,
        already_reached=prior_milestones,
    )
    if milestones:
        session.analytics_milestones = sorted(
            {int(value) for value in prior_milestones}.union(milestones)
        )

    if body.event_type in {"download_attempt", "recording_signal", "permission_change"}:
        log_security_audit_event(
            db,
            action=f"review_link.event.{body.event_type}",
            resource_type="review_event",
            resource_id=str(session.id),
            actor_type="guest",
            review_link_id=link.id,
            session_id=session.id,
            video_id=link.video_id,
            ip_address=session.ip_address,
            country_code=getattr(session, "country_code", None),
            user_agent=session.user_agent,
            metadata=getattr(body, "meta_info", None) or {},
        )
    project = db.query(Project).filter(Project.id == video.project_id).first() if video else None
    common = {
        "feature_key": "review_link",
        "project_id": video.project_id if video else None,
        "video_id": link.video_id,
        "review_link_id": link.id,
        "review_session_id": session.id,
    }
    for milestone in milestones:
        emit(
            db,
            "review_playback_milestone_reached",
            workspace_id=project.workspace_id if project else None,
            anonymous_id=f"review-session:{session.id}",
            properties={
                **common,
                "milestone_percent": milestone,
                "result": "success",
            },
            source="review_service",
            event_id=f"review-milestone:{session.id}:{milestone}",
        )
    if body.event_type == "seek":
        meta = body.meta_info if isinstance(body.meta_info, dict) else {}
        from_raw = meta.get("from_position", meta.get("from", body.position))
        to_raw = meta.get("to_position", meta.get("to", body.range_end))
        try:
            from_position = bounded_position(float(from_raw), duration)
            to_position = bounded_position(float(to_raw), duration)
        except (TypeError, ValueError):
            from_position = to_position = 0
        delta = to_position - from_position
        if abs(delta) >= 5:
            event_name = (
                "review_skip_forward_detected" if delta > 0 else "review_rewatch_detected"
            )
            emit(
                db,
                event_name,
                workspace_id=project.workspace_id if project else None,
                anonymous_id=f"review-session:{session.id}",
                properties={
                    **common,
                    "from_second": from_position,
                    "to_second": to_position,
                    "delta_seconds": abs(delta),
                    "result": "observed",
                },
                source="review_service",
            )
    db.commit()
    return {"ok": True}


def _public_comment_user(c: Comment) -> PublicReviewCommentUser:
    if c.user_id and c.user:
        return PublicReviewCommentUser(
            id=c.user.id,
            name=c.user.name or (c.user.email or "User"),
            email=c.user.email,
            avatar_url=getattr(c.user, "avatar_url", None),
            is_guest=False,
        )
    ga = getattr(c, "guest_avatar_url", None)
    if ga:
        return PublicReviewCommentUser(
            id=None,
            name=c.guest_name or "Guest",
            email=c.guest_email,
            avatar_url=ga,
            is_guest=True,
        )
    return PublicReviewCommentUser(
        id=None,
        name=c.guest_name or "Guest",
        email=c.guest_email,
        avatar_url=(
            f"https://ui-avatars.com/api/?name={(c.guest_name or 'Guest').replace(' ', '+')}&background=18181b&color=ffffff"
        ),
        is_guest=True,
    )


def _public_anchor_state(db: Session, c: Comment) -> tuple[bool, str | None, int | None]:
    seg_idx = getattr(c, "transcript_segment_index", None)
    anchor_text = (getattr(c, "anchor_text", None) or "").strip().lower()
    if seg_idx is None:
        return True, None, None
    tr = db.query(VideoTranscription).filter(VideoTranscription.video_id == c.video_id).first()
    segments = (tr.segments if tr and isinstance(tr.segments, list) else []) if tr else []
    if seg_idx < 0 or seg_idx >= len(segments):
        if anchor_text:
            for seg in segments:
                seg_text = str((seg or {}).get("text", "")).lower()
                if anchor_text in seg_text:
                    return False, "anchor_remapped", int((seg or {}).get("start", 0))
        return False, "segment_out_of_range", None
    if not anchor_text:
        return True, None, None
    seg_text = str((segments[seg_idx] or {}).get("text", "")).lower()
    if anchor_text not in seg_text:
        for seg in segments:
            candidate = str((seg or {}).get("text", "")).lower()
            if anchor_text in candidate:
                return False, "anchor_remapped", int((seg or {}).get("start", 0))
        return False, "anchor_text_mismatch", None
    return True, None, None


def _serialize_public_comment(
    c: Comment, replies: List[Comment] | None = None, db: Session | None = None
) -> PublicReviewCommentResponse:
    anchor_ok, anchor_reason, anchor_remap_timecode = (True, None, None)
    if db is not None:
        anchor_ok, anchor_reason, anchor_remap_timecode = _public_anchor_state(db, c)
    return PublicReviewCommentResponse(
        id=c.id,
        video_id=c.video_id,
        parent_id=c.parent_id,
        text=c.text,
        timecode=c.timecode,
        end_timecode=c.end_timecode,
        drawing_data=c.drawing_data,
        transcript_segment_index=getattr(c, "transcript_segment_index", None),
        word_start_index=getattr(c, "word_start_index", None),
        word_end_index=getattr(c, "word_end_index", None),
        anchor_text=getattr(c, "anchor_text", None),
        transcript_anchor_resolved=anchor_ok,
        transcript_anchor_reason=anchor_reason,
        transcript_anchor_remap_timecode=anchor_remap_timecode,
        is_resolved=c.is_resolved,
        kind=getattr(c, "kind", None) or COMMENT_KIND_COMMENT,
        status=getattr(c, "status", None) or "open",
        user=_public_comment_user(c),
        likes_count=len(c.likes) if c.likes else 0,
        replies_count=len(replies) if replies is not None else (len(c.replies) if c.replies else 0),
        created_at=c.created_at,
        updated_at=c.updated_at,
        replies=[_serialize_public_comment(r, db=db) for r in (replies or [])],
    )


def _public_comment_visible(c: Comment) -> bool:
    return is_client_visible(getattr(c, "visibility", None), c.is_private)


def _build_public_comment_tree(
    rows: List[Comment], db: Session
) -> List[PublicReviewCommentResponse]:
    by_parent: dict[int | None, List[Comment]] = defaultdict(list)
    for c in rows:
        by_parent[c.parent_id].append(c)

    def build(parent_id: int | None) -> List[PublicReviewCommentResponse]:
        children = sorted(
            by_parent.get(parent_id, []),
            key=lambda x: (x.timecode or 0, x.created_at),
        )
        out: List[PublicReviewCommentResponse] = []
        for c in children:
            if not _public_comment_visible(c):
                continue
            anchor_ok, anchor_reason, anchor_remap_timecode = _public_anchor_state(db, c)
            nested = build(c.id)
            item = PublicReviewCommentResponse(
                id=c.id,
                video_id=c.video_id,
                parent_id=c.parent_id,
                text=c.text,
                timecode=c.timecode,
                end_timecode=c.end_timecode,
                drawing_data=c.drawing_data,
                transcript_segment_index=getattr(c, "transcript_segment_index", None),
                word_start_index=getattr(c, "word_start_index", None),
                word_end_index=getattr(c, "word_end_index", None),
                anchor_text=getattr(c, "anchor_text", None),
                transcript_anchor_resolved=anchor_ok,
                transcript_anchor_reason=anchor_reason,
                transcript_anchor_remap_timecode=anchor_remap_timecode,
                is_resolved=c.is_resolved,
                kind=getattr(c, "kind", None) or COMMENT_KIND_COMMENT,
                status=getattr(c, "status", None) or "open",
                user=_public_comment_user(c),
                likes_count=len(c.likes) if c.likes else 0,
                replies_count=len(nested),
                created_at=c.created_at,
                updated_at=c.updated_at,
                replies=nested,
            )
            out.append(item)
        return out

    return build(None)


@public_router.get(
    "/{token}/comments", response_model=List[PublicReviewCommentResponse]
)
def list_public_comments(token: str, db: Session = Depends(get_db)):
    link = _get_link_or_404(token, db)
    _assert_link_usable(link)
    rows = (
        db.query(Comment)
        .filter(Comment.video_id == link.video_id)
        .options(joinedload(Comment.likes), joinedload(Comment.user))
        .order_by(Comment.created_at.asc())
        .all()
    )
    return _build_public_comment_tree(rows, db)


@public_router.get(
    "/{token}/comments/delta", response_model=PublicReviewCommentDeltaResponse
)
def public_comments_delta(
    token: str,
    session_id: int,
    since: Optional[str] = None,
    db: Session = Depends(get_db),
):
    link = _get_link_or_404(token, db)
    _assert_link_usable(link)
    session = _get_session_or_404(link, session_id, db)
    t0 = session.first_viewed_at
    if since:
        try:
            raw = since.strip().replace("Z", "+00:00")
            parsed = datetime.fromisoformat(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            t0 = parsed
        except Exception:
            pass
    rows = (
        db.query(Comment)
        .filter(
            Comment.video_id == link.video_id,
            Comment.updated_at > t0,
        )
        .order_by(Comment.updated_at.asc())
        .limit(500)
        .all()
    )
    now = datetime.now(timezone.utc)
    items = [
        PublicReviewCommentDeltaItem(
            id=r.id,
            parent_id=r.parent_id,
            updated_at=r.updated_at,
            kind=getattr(r, "kind", COMMENT_KIND_COMMENT) or COMMENT_KIND_COMMENT,
            status=getattr(r, "status", "open") or "open",
        )
        for r in rows
        if _public_comment_visible(r)
    ]
    return PublicReviewCommentDeltaResponse(items=items, server_time=now)


@public_router.get("/{token}/comments/export")
def public_export_comments(
    token: str,
    session_id: int,
    format: str = "csv",
    db: Session = Depends(get_db),
):
    link = _get_link_or_404(token, db)
    _assert_link_usable(link)
    if not getattr(link, "allow_export", False):
        raise HTTPException(status_code=403, detail="Export disabled for this link")
    _get_session_or_404(link, session_id, db)
    video = db.query(Video).filter(Video.id == link.video_id).first()
    rows = (
        db.query(Comment)
        .filter(Comment.video_id == link.video_id, Comment.review_link_id == link.id)
        .options(joinedload(Comment.user))
        .order_by(Comment.timecode.asc(), Comment.created_at.asc())
        .all()
    )
    rows = [r for r in rows if _public_comment_visible(r)]
    try:
        data, mime, filename = export_comments(rows, format, video.name if video else "Video")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return StreamingResponse(
        io.BytesIO(data),
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@public_router.post(
    "/{token}/comments", response_model=PublicReviewCommentResponse
)
async def create_public_comment(
    token: str,
    body: PublicReviewCommentCreate,
    db: Session = Depends(get_db),
):
    link = _get_link_or_404(token, db)
    _assert_link_usable(link)
    if not link.allow_comments:
        raise HTTPException(status_code=403, detail="Comments disabled on this link")
    session = _get_session_or_404(link, body.session_id, db)

    if body.parent_id is not None:
        parent = (
            db.query(Comment)
            .filter(
                Comment.id == body.parent_id,
                Comment.video_id == link.video_id,
            )
            .first()
        )
        if not parent or not is_client_visible(getattr(parent, "visibility", None), parent.is_private):
            raise HTTPException(status_code=404, detail="Parent comment not found")

    cr_kind = (getattr(body, "kind", None) or "comment").strip().lower()
    if cr_kind not in (COMMENT_KIND_COMMENT, COMMENT_KIND_CHANGE_REQUEST):
        cr_kind = COMMENT_KIND_COMMENT
    comment = Comment(
        video_id=link.video_id,
        user_id=None,
        parent_id=body.parent_id,
        text=body.text,
        timecode=body.timecode,
        end_timecode=body.end_timecode,
        drawing_data=body.drawing_data,
        transcript_segment_index=body.transcript_segment_index,
        word_start_index=body.word_start_index,
        word_end_index=body.word_end_index,
        anchor_text=body.anchor_text,
        guest_name=session.guest_name or "Guest",
        guest_email=session.guest_email,
        guest_avatar_url=session.guest_avatar_url,
        review_link_id=link.id,
        kind=cr_kind,
        status="open",
        visibility=COMMENT_VISIBILITY_PUBLIC,
        is_private=False,
    )
    sync_is_resolved_from_status(comment)
    db.add(comment)

    # Also log as an event for analytics
    db.add(
        ReviewEvent(
            session_id=session.id,
            event_type="comment",
            position=max(0, int(body.timecode or 0)),
        )
    )
    video_for_analytics = db.query(Video).filter(Video.id == link.video_id).first()
    project_for_analytics = (
        db.query(Project).filter(Project.id == video_for_analytics.project_id).first()
        if video_for_analytics
        else None
    )
    emit_once(
        db,
        "review_comment_created",
        event_id=f"review-comment:{comment.id}:created",
        workspace_id=project_for_analytics.workspace_id if project_for_analytics else None,
        anonymous_id=f"review-session:{session.id}",
        properties={
            "feature_key": "comments",
            "project_id": video_for_analytics.project_id if video_for_analytics else None,
            "video_id": link.video_id,
            "review_link_id": link.id,
            "review_session_id": session.id,
            "comment_kind": cr_kind,
            "is_reply": body.parent_id is not None,
            "has_drawing": bool(body.drawing_data),
            "has_range": body.end_timecode is not None,
            "position_second": max(0, int(body.timecode or 0)),
            "result": "success",
        },
        source="review_service",
    )
    emit_once(
        db,
        "feature_completed",
        event_id=f"feature:comments:comment:{comment.id}:created",
        user_id=link.created_by,
        workspace_id=project_for_analytics.workspace_id if project_for_analytics else None,
        anonymous_id=f"review-session:{session.id}",
        properties={
            "feature_key": "comments",
            "project_id": video_for_analytics.project_id if video_for_analytics else None,
            "video_id": link.video_id,
            "review_link_id": link.id,
            "review_session_id": session.id,
            "comment_id": comment.id,
            "actor_type": "guest",
            "completion_type": "comment_persisted",
            "result": "success",
        },
        source="review_service",
    )
    db.commit()
    db.refresh(comment)

    # Tell the team a client said something.
    #
    # This block used to sit inside `if handles:`, so a client could leave
    # twenty comments and — unless they happened to type an @mention — nobody
    # was ever told. Client feedback rotting unseen is the exact failure this
    # product exists to remove, so the owner and uploader are now notified for
    # every guest comment. Volume is handled by coalescing on `group_key`
    # rather than by staying silent.
    video = db.query(Video).filter(Video.id == link.video_id).first()
    project = (
        db.query(Project).filter(Project.id == video.project_id).first() if video else None
    )
    if project:
        actor_name = session.guest_name or session.guest_email or "Guest reviewer"
        frontend_base = os.getenv("FRONTEND_BASE_URL", "http://localhost:3000").rstrip("/")
        # Recipients are project team — deep-link to the authenticated player.
        comment_url = (
            f"{frontend_base}/player/{link.video_id}?"
            + urlencode({"tab": "comments", "commentId": str(comment.id)})
        )
        is_change_request = comment.kind == COMMENT_KIND_CHANGE_REQUEST

        specs: list[NotificationSpec] = []
        notified_ids: set[int] = set()

        handles = extract_mention_handles(body.text or "")
        if handles:
            recipients = resolve_mentioned_users(
                mention_handles=handles,
                candidate_users=list_users_for_mentions(db, project),
                actor_user_id=None,
            )
            for recipient in recipients:
                specs.append(
                    NotificationSpec(
                        user_id=recipient.id,
                        type=TYPE_MENTION,
                        project_id=project.id,
                        video_id=link.video_id,
                        comment_id=comment.id,
                        message=f"{actor_name} mentioned you in a comment",
                    )
                )
                notified_ids.add(recipient.id)
                settings = (
                    db.query(UserSettings)
                    .filter(UserSettings.user_id == recipient.id)
                    .first()
                )
                if settings is not None and not settings.email_mentions:
                    continue
                queued = enqueue_mention_email_job(
                    recipient_user_id=recipient.id,
                    actor_name=actor_name,
                    project_name=project.name,
                    video_name=video.name if video else None,
                    comment_text=comment.text or "",
                    comment_url=comment_url,
                )
                if not queued:
                    logger.warning(
                        "Mention email enqueue skipped/failed for user %s", recipient.id
                    )

        owner_ids = {project.creator_id, video.uploader_id if video else None}
        owner_ids.discard(None)
        owner_ids -= notified_ids
        video_label = video.name if video else "your video"
        for owner_id in owner_ids:
            specs.append(
                NotificationSpec(
                    user_id=owner_id,
                    type=TYPE_CLIENT_COMMENT,
                    project_id=project.id,
                    video_id=link.video_id,
                    comment_id=comment.id,
                    message=(
                        f"{actor_name} requested a change on {video_label}"
                        if is_change_request
                        else f"{actor_name} commented on {video_label}"
                    ),
                    # One alert per reviewer per video per window, however many
                    # comments they leave in a sitting.
                    group_key=f"client_comment:{link.video_id}:{session.id}",
                )
            )
            if wants_comment_emails(db, owner_id):
                enqueue_comment_notification_email_job(
                    recipient_user_id=owner_id,
                    actor_name=actor_name,
                    project_name=project.name,
                    video_name=video.name if video else None,
                    comment_text=comment.text or "",
                    comment_url=comment_url,
                )

        await emit_notifications(db, specs)

    reply_rows = (
        db.query(Comment)
        .filter(
            Comment.parent_id == comment.id,
            Comment.visibility == COMMENT_VISIBILITY_PUBLIC,
            Comment.is_private == False,  # noqa: E712
        )
        .order_by(Comment.created_at.asc())
        .all()
    )
    return _serialize_public_comment(comment, reply_rows, db=db)


@public_router.get("/{token}/comments/grouped", response_model=List[ReviewSceneGroup])
def grouped_public_comments(token: str, db: Session = Depends(get_db)):
    link = _get_link_or_404(token, db)
    _assert_link_usable(link)
    comments = (
        db.query(Comment)
        .filter(
            Comment.video_id == link.video_id,
            Comment.parent_id.is_(None),
            Comment.visibility == COMMENT_VISIBILITY_PUBLIC,
            Comment.is_private == False,  # noqa: E712
        )
        .order_by(Comment.timecode.asc())
        .all()
    )
    return build_review_scene_groups(db, link.video_id, comments)


@public_router.post("/{token}/magic-link/send")
def send_magic_link(
    token: str,
    body: PublicReviewMagicSendRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    link = _get_link_or_404(token, db)
    _assert_link_usable(link)
    if not link.require_email:
        return {"ok": True, "message": "Email verification is not required for this link"}

    recent_count = (
        db.query(func.count(ReviewMagicToken.id))
        .filter(
            ReviewMagicToken.review_link_id == link.id,
            ReviewMagicToken.email == body.email,
            ReviewMagicToken.created_at >= datetime.now(timezone.utc) - timedelta(minutes=10),
        )
        .scalar()
        or 0
    )
    if recent_count >= 5:
        raise HTTPException(status_code=429, detail="Too many magic-link requests")

    raw_token = secrets.token_urlsafe(32)
    verify_url = f"{request.base_url}api/review/{link.token}?magic_token={raw_token}"
    rec = ReviewMagicToken(
        review_link_id=link.id,
        email=body.email.lower().strip(),
        guest_name=(body.guest_name or "").strip() or None,
        token_hash=_hash_magic_token(raw_token),
        fingerprint=body.fingerprint,
        ip_address=request.client.host if request.client else None,
        source="self_service",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=20),
    )
    db.add(rec)
    db.commit()
    sent = send_review_magic_link_email(
        to_email=rec.email,
        review_label=link.label or "Video review",
        verify_url=verify_url,
        expires_minutes=20,
        recipient_name=rec.guest_name,
    )
    return {"ok": bool(sent)}


@public_router.post("/{token}/magic-link/verify", response_model=PublicReviewAuthResponse)
def verify_magic_link(
    token: str,
    body: PublicReviewMagicVerifyRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    link = _get_link_or_404(token, db)
    _assert_link_usable(link)
    rec = (
        db.query(ReviewMagicToken)
        .filter(
            ReviewMagicToken.review_link_id == link.id,
            ReviewMagicToken.token_hash == _hash_magic_token(body.magic_token),
        )
        .first()
    )
    if not rec:
        return PublicReviewAuthResponse(ok=False, error="Invalid magic link")
    now = datetime.now(timezone.utc)
    exp = rec.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if rec.used_at is not None or exp < now:
        return PublicReviewAuthResponse(ok=False, error="Magic link expired")

    rec.used_at = now
    db.commit()
    return start_session(
        token=token,
        body=PublicReviewAuthRequest(
            fingerprint=body.fingerprint,
            guest_name=body.guest_name or rec.guest_name,
            guest_email=rec.email,
            guest_avatar_url=body.guest_avatar_url,
        ),
        request=request,
        db=db,
    )


@public_router.post("/{token}/draft", response_model=PublicReviewDraftResponse)
def save_draft(
    token: str,
    body: PublicReviewDraftRequest,
    db: Session = Depends(get_db),
):
    link = _get_link_or_404(token, db)
    _assert_link_usable(link)
    session = _get_session_or_404(link, body.session_id, db)
    draft = (
        db.query(ReviewCommentDraft)
        .filter(
            ReviewCommentDraft.review_link_id == link.id,
            ReviewCommentDraft.session_id == session.id,
            ReviewCommentDraft.video_id == link.video_id,
        )
        .first()
    )
    if draft:
        draft.text = body.text
        draft.timecode = max(0, int(body.timecode))
        draft.updated_at = datetime.now(timezone.utc)
    else:
        draft = ReviewCommentDraft(
            review_link_id=link.id,
            session_id=session.id,
            video_id=link.video_id,
            text=body.text,
            timecode=max(0, int(body.timecode)),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(draft)
    db.commit()
    db.refresh(draft)
    return PublicReviewDraftResponse(
        text=draft.text,
        timecode=draft.timecode,
        updated_at=draft.updated_at,
    )


@public_router.get("/{token}/draft/{session_id}", response_model=PublicReviewDraftResponse)
def get_draft(token: str, session_id: int, db: Session = Depends(get_db)):
    link = _get_link_or_404(token, db)
    _assert_link_usable(link)
    _get_session_or_404(link, session_id, db)
    draft = (
        db.query(ReviewCommentDraft)
        .filter(
            ReviewCommentDraft.review_link_id == link.id,
            ReviewCommentDraft.session_id == session_id,
            ReviewCommentDraft.video_id == link.video_id,
        )
        .first()
    )
    if not draft:
        return PublicReviewDraftResponse(
            text="",
            timecode=0,
            updated_at=datetime.now(timezone.utc),
        )
    return PublicReviewDraftResponse(
        text=draft.text,
        timecode=draft.timecode,
        updated_at=draft.updated_at,
    )


@public_router.post("/{token}/approve")
async def approve(
    token: str,
    body: PublicReviewApproveRequest,
    db: Session = Depends(get_db),
):
    """A guest signs off on the cut they are watching.

    This used to write `ReviewSession.approved_at` and stop there, so a client
    could approve a video whose status stayed "in progress" forever and whose
    editor saw no change anywhere in the app. The decision now lands in
    `video_approvals` and moves `Video.status`, which is what makes team
    approval and guest approval one mechanism instead of two.

    The handler is `async` so it can reach the notification WebSocket; as a
    sync handler it could only ever enqueue a push.
    """
    link = _get_link_or_404(token, db)
    _assert_link_usable(link)
    session = _get_session_or_404(link, body.session_id, db)

    video = db.query(Video).filter(Video.id == link.video_id).first()
    project = (
        db.query(Project).filter(Project.id == video.project_id).first() if video else None
    )

    if not body.approved:
        # Withdrawing an approval puts the cut back in front of reviewers.
        session.approved_at = None
        if video is not None and is_approved(video):
            apply_video_status(
                db, video, STATUS_IN_REVIEW, skip_transition_check=True
            )
        db.commit()
        return {"ok": True, "approved_at": None}

    blockers = client_approve_blockers(db, link)
    if blockers:
        raise HTTPException(
            status_code=400,
            detail=blockers[0].get("message", "Approval blocked"),
        )

    session.approved_at = datetime.now(timezone.utc)
    if video is not None:
        record_decision(
            db,
            video,
            DECISION_APPROVED,
            review_session_id=session.id,
            review_link_id=link.id,
            note=getattr(body, "note", None),
            # A guest can receive a direct link before the editor formally
            # moves the cut to in-review; their explicit decision is still real.
            skip_transition_check=True,
        )
    db.commit()

    if project is not None:
        approver = session.guest_name or session.guest_email or "Your client"
        video_label = video.name if video else "the video"
        recipients = {project.creator_id, video.uploader_id if video else None}
        recipients.discard(None)
        await emit_notifications(
            db,
            [
                NotificationSpec(
                    user_id=uid,
                    type=TYPE_APPROVAL,
                    project_id=project.id,
                    video_id=link.video_id,
                    message=f"{approver} approved {video_label}",
                )
                for uid in recipients
            ],
        )

    return {"ok": True, "approved_at": session.approved_at}


@public_router.post("/{token}/request-changes")
async def request_changes(
    token: str,
    body: PublicReviewRequestChangesRequest,
    db: Session = Depends(get_db),
):
    """A guest sends the cut back.

    Until now there was no reject action anywhere in the product — the only
    way a reviewer could express "not yet" was to write a change-request
    comment and hope somebody noticed. Approving was a button; declining was
    an inference.

    The note optionally becomes a change request at t=0 so it lands in the
    editor's revision checklist instead of living only in a status field.
    """
    link = _get_link_or_404(token, db)
    _assert_link_usable(link)
    session = _get_session_or_404(link, body.session_id, db)

    video = db.query(Video).filter(Video.id == link.video_id).first()
    if video is None:
        raise HTTPException(status_code=404, detail="Video not found")
    project = db.query(Project).filter(Project.id == video.project_id).first()

    note = (body.note or "").strip() or None

    # Requesting changes withdraws any prior sign-off from this guest.
    session.approved_at = None

    record_decision(
        db,
        video,
        DECISION_CHANGES_REQUESTED,
        review_session_id=session.id,
        review_link_id=link.id,
        note=note,
        skip_transition_check=True,
    )

    created_comment: Comment | None = None
    if note and body.create_comment and link.allow_comments:
        created_comment = Comment(
            video_id=video.id,
            user_id=None,
            text=note,
            timecode=0,
            guest_name=session.guest_name or "Guest",
            guest_email=session.guest_email,
            guest_avatar_url=session.guest_avatar_url,
            review_link_id=link.id,
            kind=COMMENT_KIND_CHANGE_REQUEST,
            status="open",
            visibility=COMMENT_VISIBILITY_PUBLIC,
            is_private=False,
        )
        sync_is_resolved_from_status(created_comment)
        db.add(created_comment)

    emit(
        db,
        "review_change_requested",
        workspace_id=project.workspace_id if project else None,
        anonymous_id=f"review-session:{session.id}",
        properties={
            "feature_key": "approval",
            "project_id": video.project_id,
            "video_id": video.id,
            "review_link_id": link.id,
            "review_session_id": session.id,
            "has_note": bool(note),
            "comment_created": created_comment is not None,
            "result": "changes_requested",
        },
        source="review_service",
    )

    db.commit()

    if project is not None:
        reviewer = session.guest_name or session.guest_email or "Your client"
        recipients = {project.creator_id, video.uploader_id}
        recipients.discard(None)
        await emit_notifications(
            db,
            [
                NotificationSpec(
                    user_id=uid,
                    type=TYPE_CHANGES_REQUESTED,
                    project_id=project.id,
                    video_id=video.id,
                    comment_id=created_comment.id if created_comment else None,
                    message=f"{reviewer} requested changes on {video.name}",
                )
                for uid in recipients
            ],
        )

    return {
        "ok": True,
        "status": video.status,
        "comment_id": created_comment.id if created_comment else None,
    }


@public_router.post("/{token}/signoff", response_model=PublicReviewSignoffResponse)
def signoff(
    token: str,
    body: PublicReviewSignoffRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    from app.services.review_signoff_pdf import build_review_signoff_pdf_bytes, upload_review_signoff_pdf

    link = _get_link_or_404(token, db)
    _assert_link_usable(link)
    session = _get_session_or_404(link, body.session_id, db)
    img = (body.signature_image_data or "").strip()
    typed = (body.typed_signature or "").strip()
    sig_type = "none"
    if img:
        sig_type = "drawn"
    elif typed:
        sig_type = "typed"
    sig_payload = img or typed or "—"
    signed_at = datetime.now(timezone.utc)
    record = ReviewSignoff(
        review_link_id=link.id,
        session_id=session.id,
        signer_name=session.guest_name,
        signer_email=session.guest_email,
        declaration_text=body.declaration_text.strip(),
        legal_snapshot_json={
            "ip_address": request.client.host if request.client else None,
            "user_agent": request.headers.get("user-agent"),
            "approved_at": session.approved_at.isoformat() if session.approved_at else None,
        },
        signed_at=signed_at,
        signature_type=sig_type,
        typed_signature=typed or None,
        signature_image_data=img or None,
    )
    db.add(record)
    db.flush()
    try:
        pdf_bytes = build_review_signoff_pdf_bytes(
            body.declaration_text.strip(),
            record.signer_name or "",
            record.signer_email or "",
            sig_payload,
            record.signed_at,
        )
        record.pdf_url = upload_review_signoff_pdf(pdf_bytes, record.id)
    except Exception:
        logger.exception("Review sign-off PDF failed")
    video = db.query(Video).filter(Video.id == link.video_id).first()
    project = db.query(Project).filter(Project.id == video.project_id).first() if video else None
    emit(
        db,
        "review_signoff_created",
        workspace_id=project.workspace_id if project else None,
        anonymous_id=f"review-session:{session.id}",
        properties={
            "feature_key": "signoff",
            "project_id": video.project_id if video else None,
            "video_id": link.video_id,
            "review_link_id": link.id,
            "review_session_id": session.id,
            "signature_type": sig_type,
            "pdf_created": bool(record.pdf_url),
            "result": "success",
        },
        source="review_service",
    )
    emit(
        db,
        "feature_completed",
        workspace_id=project.workspace_id if project else None,
        anonymous_id=f"review-session:{session.id}",
        properties={
            "feature_key": "signoff",
            "project_id": video.project_id if video else None,
            "video_id": link.video_id,
            "review_link_id": link.id,
            "review_session_id": session.id,
            "completion_type": "review_signoff",
            "result": "success",
        },
        source="review_service",
    )
    emit(
        db,
        "feature_result_used",
        workspace_id=project.workspace_id if project else None,
        anonymous_id=f"review-session:{session.id}",
        properties={
            "feature_key": "signoff",
            "project_id": video.project_id if video else None,
            "video_id": link.video_id,
            "review_link_id": link.id,
            "review_session_id": session.id,
            "result_action": "review_cycle_advanced",
            "result": "success",
        },
        source="review_service",
    )
    db.commit()
    db.refresh(record)
    return PublicReviewSignoffResponse(
        id=record.id,
        ok=True,
        signed_at=record.signed_at,
        signer_name=record.signer_name,
        signer_email=record.signer_email,
        declaration_text=record.declaration_text,
        pdf_url=record.pdf_url,
    )


@public_router.get(
    "/{token}/room/messages", response_model=List[PublicReviewRoomMessageResponse]
)
def list_room_messages(token: str, db: Session = Depends(get_db)):
    link = _get_link_or_404(token, db)
    _assert_link_usable(link)
    rows = (
        db.query(ReviewRoomMessage, ReviewSession)
        .join(ReviewSession, ReviewSession.id == ReviewRoomMessage.session_id)
        .filter(ReviewRoomMessage.review_link_id == link.id)
        .order_by(ReviewRoomMessage.created_at.asc())
        .limit(500)
        .all()
    )
    return [
        PublicReviewRoomMessageResponse(
            id=msg.id,
            session_id=msg.session_id,
            guest_name=sess.guest_name if sess else None,
            guest_avatar_url=sess.guest_avatar_url if sess else None,
            body=msg.body,
            created_at=msg.created_at,
        )
        for msg, sess in rows
    ]


@public_router.post(
    "/{token}/room/messages", response_model=PublicReviewRoomMessageResponse
)
def create_room_message(
    token: str,
    body: PublicReviewRoomMessageCreate,
    db: Session = Depends(get_db),
):
    link = _get_link_or_404(token, db)
    _assert_link_usable(link)
    session = _get_session_or_404(link, body.session_id, db)
    text = (body.body or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message body required")
    row = ReviewRoomMessage(
        review_link_id=link.id,
        session_id=session.id,
        body=text[:5000],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return PublicReviewRoomMessageResponse(
        id=row.id,
        session_id=session.id,
        guest_name=session.guest_name,
        guest_avatar_url=session.guest_avatar_url,
        body=row.body,
        created_at=row.created_at,
    )


@public_router.post(
    "/{token}/recordings", response_model=PublicReviewRecordingResponse
)
def create_recording_session(
    token: str,
    body: PublicReviewRecordingCreate,
    db: Session = Depends(get_db),
):
    if os.getenv("FEATURE_REVIEW_RECORDING", "1").strip() in ("0", "false", "False"):
        raise HTTPException(status_code=404, detail="Recording is disabled")
    link = _get_link_or_404(token, db)
    _assert_link_usable(link)
    session = _get_session_or_404(link, body.session_id, db)
    row = ReviewRecordingSession(
        review_link_id=link.id,
        session_id=session.id,
        consent_snapshot=body.consent_snapshot,
        started_at=body.started_at,
        ended_at=body.ended_at,
        status="processing",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return PublicReviewRecordingResponse(
        id=row.id,
        session_id=row.session_id,
        status=row.status,
        file_url=row.file_url,
        mime_type=row.mime_type,
        bytes_size=row.bytes_size,
        started_at=row.started_at,
        ended_at=row.ended_at,
        archived_at=row.archived_at,
        deleted_at=row.deleted_at,
        retention_days=row.retention_days,
        created_at=row.created_at,
    )


@public_router.post(
    "/{token}/recordings/{recording_id}/upload", response_model=PublicReviewRecordingResponse
)
def upload_recording_media(
    token: str,
    recording_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    if os.getenv("FEATURE_REVIEW_RECORDING", "1").strip() in ("0", "false", "False"):
        raise HTTPException(status_code=404, detail="Recording is disabled")
    link = _get_link_or_404(token, db)
    _assert_link_usable(link)
    row = (
        db.query(ReviewRecordingSession)
        .filter(
            ReviewRecordingSession.id == recording_id,
            ReviewRecordingSession.review_link_id == link.id,
        )
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Recording not found")
    url = upload_file_to_cloudinary(file, resource_type="video")
    row.file_url = url
    row.storage_key = url
    row.status = "ready"
    row.mime_type = file.content_type
    try:
        data = file.file.read()
        row.bytes_size = len(data)
    except Exception:
        row.bytes_size = row.bytes_size
    db.commit()
    db.refresh(row)
    return PublicReviewRecordingResponse(
        id=row.id,
        session_id=row.session_id,
        status=row.status,
        file_url=row.file_url,
        mime_type=row.mime_type,
        bytes_size=row.bytes_size,
        started_at=row.started_at,
        ended_at=row.ended_at,
        archived_at=row.archived_at,
        deleted_at=row.deleted_at,
        retention_days=row.retention_days,
        created_at=row.created_at,
    )


@public_router.get(
    "/{token}/recordings", response_model=List[PublicReviewRecordingResponse]
)
def list_recordings(token: str, db: Session = Depends(get_db)):
    if os.getenv("FEATURE_REVIEW_RECORDING", "1").strip() in ("0", "false", "False"):
        return []
    link = _get_link_or_404(token, db)
    _assert_link_usable(link)
    rows = (
        db.query(ReviewRecordingSession)
        .filter(
            ReviewRecordingSession.review_link_id == link.id,
            ReviewRecordingSession.deleted_at.is_(None),
        )
        .order_by(ReviewRecordingSession.created_at.desc())
        .all()
    )
    return [
        PublicReviewRecordingResponse(
            id=row.id,
            session_id=row.session_id,
            status=row.status,
            file_url=row.file_url,
            mime_type=row.mime_type,
            bytes_size=row.bytes_size,
            started_at=row.started_at,
            ended_at=row.ended_at,
            archived_at=row.archived_at,
            deleted_at=row.deleted_at,
            retention_days=row.retention_days,
            created_at=row.created_at,
        )
        for row in rows
    ]


@public_router.get("/{token}/signoff", response_model=List[PublicReviewSignoffResponse])
def list_signoffs(token: str, db: Session = Depends(get_db)):
    link = _get_link_or_404(token, db)
    _assert_link_usable(link)
    rows = (
        db.query(ReviewSignoff)
        .filter(ReviewSignoff.review_link_id == link.id)
        .order_by(ReviewSignoff.signed_at.desc())
        .all()
    )
    return [
        PublicReviewSignoffResponse(
            id=row.id,
            ok=True,
            signed_at=row.signed_at,
            signer_name=row.signer_name,
            signer_email=row.signer_email,
            declaration_text=row.declaration_text,
            pdf_url=row.pdf_url,
        )
        for row in rows
    ]


@public_router.get("/{token}/signoff/{signoff_id}/pdf")
def download_signoff_pdf(token: str, signoff_id: int, db: Session = Depends(get_db)):
    link = _get_link_or_404(token, db)
    _assert_link_usable(link)
    row = (
        db.query(ReviewSignoff)
        .filter(ReviewSignoff.id == signoff_id, ReviewSignoff.review_link_id == link.id)
        .first()
    )
    if not row or not row.pdf_url:
        raise HTTPException(status_code=404, detail="PDF not available")
    return RedirectResponse(url=row.pdf_url, status_code=302)


@public_router.get("/{token}/download-allowed")
def public_download_allowed(
    token: str, session_id: Optional[int] = None, db: Session = Depends(get_db)
):
    link = _get_link_or_404(token, db)
    _assert_link_usable(link)
    session = None
    if session_id is not None:
        session = _get_session_or_404(link, session_id, db)
    video = db.query(Video).filter(Video.id == link.video_id).first()
    return {
        "ok": True,
        "allowed": _is_session_download_unlocked(link, session, db=db, video=video),
    }


@public_router.get("/{token}/versions")
def list_review_versions(token: str, db: Session = Depends(get_db)):
    """Sibling review links, so a guest can tell there is a newer cut.

    This used to raise `AttributeError` on every single call — it reached for
    `link.project` and `link.project_id`, and `ReviewLink` has neither a
    `project` relationship nor a `project_id` column. A link's route to its
    project is through its video, which is how `app/api/video_payload.py`
    already does it.
    """
    link = _get_link_or_404(token, db)
    _assert_link_usable(link)

    video = db.query(Video).filter(Video.id == link.video_id).first()
    project_id = video.project_id if video else None
    project_type = None
    if project_id:
        project_type = (
            db.query(Project.project_type).filter(Project.id == project_id).scalar()
        )

    if project_type == "review":
        # Review projects treat every video in the project as a version of the
        # same deliverable, so peers are found through the project.
        peers = (
            db.query(ReviewLink)
            .join(Video, Video.id == ReviewLink.video_id)
            .filter(
                Video.project_id == project_id,
                ReviewLink.revoked_at.is_(None),
            )
            .order_by(ReviewLink.created_at.asc())
            .all()
        )
    elif link.version_group_id:
        peers = (
            db.query(ReviewLink)
            .filter(
                ReviewLink.version_group_id == link.version_group_id,
                ReviewLink.revoked_at.is_(None),
            )
            .order_by(ReviewLink.created_at.asc())
            .all()
        )
    elif video is not None and video.version_group_id:
        # Links created before version_group_id was stamped on them still have
        # a chain — it just lives on the video.
        peers = (
            db.query(ReviewLink)
            .join(Video, Video.id == ReviewLink.video_id)
            .filter(
                Video.project_id == project_id,
                Video.version_group_id == video.version_group_id,
                ReviewLink.revoked_at.is_(None),
            )
            .order_by(ReviewLink.created_at.asc())
            .all()
        )
    else:
        peers = [link]

    version_by_video = {
        v.id: v
        for v in db.query(Video)
        .filter(Video.id.in_([row.video_id for row in peers] or [0]))
        .all()
    }

    items = []
    for row in peers:
        peer_video = version_by_video.get(row.video_id)
        items.append(
            {
                "id": row.id,
                "token": row.token,
                "label": row.label,
                "version_label": row.version_label,
                "created_at": row.created_at,
                "video_id": row.video_id,
                "version": peer_video.version if peer_video else None,
                "status": (peer_video.status or "in_progress") if peer_video else None,
                "is_current": row.id == link.id,
            }
        )
    items.sort(key=lambda item: (item["version"] or 0))

    latest = items[-1] if items else None
    return {
        "ok": True,
        "items": items,
        # Lets the guest page say "v4 is available — you're viewing v2".
        "current_version": next(
            (item["version"] for item in items if item["is_current"]), None
        ),
        "latest_version": latest["version"] if latest else None,
        "latest_token": latest["token"] if latest else None,
    }
