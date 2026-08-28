from __future__ import annotations

import os
import re
import hashlib
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.models.product_analytics import (
    AnalyticsConfigResponse,
    AnalyticsConsentResponse,
    AnalyticsConsentUpdate,
    AnalyticsDeletionResponse,
    AnalyticsFeedbackCreate,
    AnalyticsFeedbackResponse,
    DashboardFirstViewResponse,
    ProjectSetupFailureCreate,
)
from app.db.database import get_db
from app.db.models import (
    AnalyticsConsent,
    AnalyticsConsentEvent,
    AnalyticsDataRequest,
    AnalyticsFeedback,
    AnalyticsOutbox,
    User,
    UserSettings,
    WorkspaceMember,
    Project,
)
from app.jobs.queue import enqueue_analytics_delivery_job, enqueue_analytics_privacy_job
from app.services.analytics_events import FEATURE_KEYS
from app.services.analytics_privacy import (
    AnalyticsPrivacyError,
    decrypt_restricted_comment,
    encrypt_restricted_comment,
)
from app.services.product_analytics import emit, emit_once
from app.services.analytics_data_rights import request_analytics_deletion
from app.services.analytics_quality import build_analytics_quality_report
from app.services.project_access import can_access_project
from app.utils.security import (
    API_TOKEN_PREFIX,
    authenticate_access_token,
    authenticate_api_token,
    get_current_user,
)


router = APIRouter(prefix="/analytics", tags=["Product Analytics"])
optional_bearer = HTTPBearer(auto_error=False)
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{16,128}$")
_SAFE_ROUTE_RE = re.compile(r"^/[A-Za-z0-9_/:.\-]*$")
_PROMPT_KEYS = {
    "onboarding_abandonment",
    "project_creation_abandonment",
    "repeated_job_failure",
    "checkout_canceled",
    "subscription_canceled",
    "inactive_paid_return",
}
_REASON_CODES = {
    "price",
    "missing_feature",
    "too_complex",
    "bug",
    "slow",
    "output_quality",
    "quota",
    "temporary_need",
    "security_privacy",
    "payment_problem",
    "not_ready",
    "other",
}


def _consent_version() -> str:
    return (os.getenv("ANALYTICS_CONSENT_VERSION") or "2026-08-27").strip()


def _region_policy() -> str:
    return (os.getenv("ANALYTICS_REGION_POLICY") or "default").strip()


def _optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(optional_bearer),
    db: Session = Depends(get_db),
) -> User | None:
    if not credentials or credentials.scheme.lower() != "bearer":
        return None
    token = credentials.credentials
    try:
        if token.startswith(API_TOKEN_PREFIX):
            return authenticate_api_token(db, token)
        return authenticate_access_token(db, token, touch_session=False)
    except HTTPException:
        return None


def _consent_response(row: AnalyticsConsent) -> AnalyticsConsentResponse:
    return AnalyticsConsentResponse.model_validate(row)


def _consent_row(
    db: Session,
    *,
    user: User | None,
    anonymous_id: str,
) -> AnalyticsConsent | None:
    exact = (
        db.query(AnalyticsConsent)
        .filter(AnalyticsConsent.anonymous_consent_id == anonymous_id)
        .first()
    )
    if exact and exact.user_id is not None and (user is None or exact.user_id != user.id):
        return None
    if exact or user is None:
        return exact
    return (
        db.query(AnalyticsConsent)
        .filter(AnalyticsConsent.user_id == user.id)
        .order_by(AnalyticsConsent.updated_at.desc(), AnalyticsConsent.id.desc())
        .first()
    )


@router.get("/config", response_model=AnalyticsConfigResponse)
def analytics_config() -> AnalyticsConfigResponse:
    return AnalyticsConfigResponse(
        consent_version=_consent_version(),
        region_policy=_region_policy(),
        product_analytics_configured=bool(
            (os.getenv("POSTHOG_PROJECT_API_KEY") or "").strip()
        ),
    )


@router.get("/consent", response_model=AnalyticsConsentResponse | None)
def get_analytics_consent(
    anonymous_consent_id: str = Query(min_length=16, max_length=128),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(_optional_user),
):
    if not _SAFE_ID_RE.fullmatch(anonymous_consent_id):
        raise HTTPException(status_code=400, detail="Invalid consent identifier")
    row = _consent_row(db, user=current_user, anonymous_id=anonymous_consent_id)
    return _consent_response(row) if row else None


@router.put("/consent", response_model=AnalyticsConsentResponse)
def update_analytics_consent(
    data: AnalyticsConsentUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(_optional_user),
):
    if not _SAFE_ID_RE.fullmatch(data.anonymous_consent_id):
        raise HTTPException(status_code=400, detail="Invalid consent identifier")
    if data.consent_version != _consent_version():
        raise HTTPException(status_code=409, detail="Consent notice changed; review it again")
    if data.region_policy != _region_policy():
        raise HTTPException(status_code=409, detail="Consent region policy changed; review it again")
    if data.replay_enabled and not data.analytics_enabled:
        raise HTTPException(status_code=400, detail="Replay requires analytics consent")
    if data.product_data_improvement_enabled and current_user is None:
        raise HTTPException(
            status_code=401,
            detail="Sign in before enabling project-data improvement",
        )

    gpc = data.global_privacy_control or request.headers.get("sec-gpc") == "1"
    analytics_enabled = False if gpc else data.analytics_enabled
    replay_enabled = False if gpc else data.replay_enabled
    product_data_enabled = False if gpc else data.product_data_improvement_enabled

    row = (
        db.query(AnalyticsConsent)
        .filter(AnalyticsConsent.anonymous_consent_id == data.anonymous_consent_id)
        .first()
    )
    if row is not None and row.user_id is not None:
        if current_user is None or row.user_id != current_user.id:
            raise HTTPException(status_code=409, detail="Consent identifier is already in use")
    if row is None:
        row = AnalyticsConsent(
            user_id=current_user.id if current_user else None,
            anonymous_consent_id=data.anonymous_consent_id,
            consent_version=data.consent_version,
        )
        db.add(row)
    elif current_user is not None:
        row.user_id = current_user.id

    now = datetime.utcnow()
    row.analytics_enabled = analytics_enabled
    row.replay_enabled = replay_enabled
    row.product_data_improvement_enabled = product_data_enabled
    row.consent_state = (
        "analytics_and_replay"
        if replay_enabled
        else "analytics" if analytics_enabled else "essential_only"
    )
    row.consent_version = data.consent_version
    row.region_policy = data.region_policy
    row.global_privacy_control = gpc
    row.consented_at = now if (analytics_enabled or replay_enabled or product_data_enabled) else None
    row.withdrawn_at = None if row.consented_at else now
    if current_user is not None:
        settings = (
            db.query(UserSettings).filter(UserSettings.user_id == current_user.id).first()
        )
        if settings is None:
            settings = UserSettings(user_id=current_user.id)
            db.add(settings)
        settings.share_data = product_data_enabled
    db.add(
        AnalyticsConsentEvent(
            user_id=current_user.id if current_user else None,
            anonymous_consent_id=data.anonymous_consent_id,
            consent_state=row.consent_state,
            analytics_enabled=analytics_enabled,
            replay_enabled=replay_enabled,
            product_data_improvement_enabled=product_data_enabled,
            consent_version=data.consent_version,
            region_policy=data.region_policy,
            global_privacy_control=gpc,
        )
    )

    if analytics_enabled:
        emit(
            db,
            "analytics_consent_updated",
            user=current_user,
            anonymous_id=data.anonymous_consent_id,
            properties={
                "consent_state": row.consent_state,
                "consent_version": row.consent_version,
                "replay_enabled": replay_enabled,
                "product_data_improvement_enabled": product_data_enabled,
                "global_privacy_control": gpc,
            },
        )
    db.commit()
    db.refresh(row)
    enqueue_analytics_delivery_job()
    return _consent_response(row)


@router.post("/feedback", response_model=AnalyticsFeedbackResponse, status_code=201)
def create_analytics_feedback(
    data: AnalyticsFeedbackCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if data.prompt_key not in _PROMPT_KEYS:
        raise HTTPException(status_code=400, detail="Invalid feedback prompt")
    if data.reason_code not in _REASON_CODES:
        raise HTTPException(status_code=400, detail="Invalid feedback reason")
    if data.feature_key and data.feature_key not in FEATURE_KEYS:
        raise HTTPException(status_code=400, detail="Invalid feature key")
    if data.route_template and not _SAFE_ROUTE_RE.fullmatch(data.route_template):
        raise HTTPException(status_code=400, detail="Invalid route template")
    if data.workspace_id is not None:
        member = (
            db.query(WorkspaceMember.id)
            .filter(
                WorkspaceMember.workspace_id == data.workspace_id,
                WorkspaceMember.user_id == current_user.id,
            )
            .first()
        )
        if not member:
            raise HTTPException(status_code=403, detail="Not a workspace member")
    if not data.user_initiated:
        cooldown_days = max(1, int(os.getenv("ANALYTICS_FEEDBACK_COOLDOWN_DAYS", "30")))
        recent = (
            db.query(AnalyticsFeedback)
            .filter(
                AnalyticsFeedback.user_id == current_user.id,
                AnalyticsFeedback.created_at >= datetime.utcnow() - timedelta(days=cooldown_days),
            )
            .order_by(AnalyticsFeedback.created_at.desc())
            .first()
        )
        if recent is not None:
            return AnalyticsFeedbackResponse(
                id=recent.id,
                accepted=False,
                comment_saved=False,
            )
    try:
        encrypted_comment = encrypt_restricted_comment(data.comment)
    except AnalyticsPrivacyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    row = AnalyticsFeedback(
        user_id=current_user.id,
        workspace_id=data.workspace_id,
        prompt_key=data.prompt_key,
        reason_code=data.reason_code,
        comment_encrypted=encrypted_comment,
        route_template=data.route_template,
        feature_key=data.feature_key,
        analytics_session_id=data.analytics_session_id,
        consent_version=data.consent_version,
    )
    db.add(row)
    db.flush()
    emit(
        db,
        "abandonment_feedback_submitted",
        user=current_user,
        workspace_id=data.workspace_id,
        properties={
            "prompt_key": data.prompt_key,
            "reason_code": data.reason_code,
            "feature_key": data.feature_key,
            "route_template": data.route_template,
            "has_comment": bool(encrypted_comment),
        },
    )
    db.commit()
    db.refresh(row)
    enqueue_analytics_delivery_job()
    return AnalyticsFeedbackResponse(
        id=row.id,
        comment_saved=bool(encrypted_comment),
    )


@router.post("/project-setup-failure", status_code=202)
def record_project_setup_failure(
    data: ProjectSetupFailureCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = db.query(Project).filter(Project.id == data.project_id).first()
    if project is None or not can_access_project(db, current_user.id, project):
        raise HTTPException(status_code=404, detail="Project not found")
    feature_key = {
        "media": "media_import",
        "video": "media_import",
        "auto-edit": "rough_cut",
        "clips": "clip_suggestions",
    }.get(data.step_key)
    properties = {
        "project_id": project.id,
        "step_key": data.step_key,
        "error_code": data.error_code,
        "failure_class": "setup",
        "feature_key": feature_key,
        "result": "failure",
    }
    emit(
        db,
        "project_setup_failed",
        user=current_user,
        workspace_id=project.workspace_id,
        properties=properties,
    )
    if feature_key:
        emit(
            db,
            "feature_failed",
            user=current_user,
            workspace_id=project.workspace_id,
            properties=properties,
        )
    db.commit()
    enqueue_analytics_delivery_job()
    return {"accepted": True}


@router.post("/dashboard-first-view", response_model=DashboardFirstViewResponse)
def record_dashboard_first_view(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Atomically record the first real dashboard render for this account."""

    now = datetime.utcnow()
    first_view = bool(
        db.query(User)
        .filter(User.id == current_user.id, User.first_dashboard_viewed_at.is_(None))
        .update({User.first_dashboard_viewed_at: now}, synchronize_session=False)
    )
    viewed_at = now if first_view else current_user.first_dashboard_viewed_at or now
    created_at = current_user.created_at or viewed_at
    elapsed_ms = max(0, int((viewed_at - created_at).total_seconds() * 1000))
    if first_view:
        emit_once(
            db,
            "dashboard_first_viewed",
            event_id=f"user:{current_user.id}:dashboard:first-view",
            user=current_user,
            properties={"time_since_account_creation_ms": elapsed_ms},
        )
    db.commit()
    if first_view:
        enqueue_analytics_delivery_job()
    return DashboardFirstViewResponse(
        first_view=first_view,
        viewed_at=viewed_at,
        time_since_account_creation_ms=elapsed_ms,
    )


@router.get("/me/export")
def export_my_analytics(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    consent = db.query(AnalyticsConsent).filter(AnalyticsConsent.user_id == current_user.id).first()
    feedback = (
        db.query(AnalyticsFeedback)
        .filter(AnalyticsFeedback.user_id == current_user.id)
        .order_by(AnalyticsFeedback.created_at.asc())
        .all()
    )
    events = (
        db.query(AnalyticsOutbox)
        .filter(AnalyticsOutbox.user_id == current_user.id)
        .order_by(AnalyticsOutbox.occurred_at.asc())
        .limit(10_000)
        .all()
    )
    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "consent": _consent_response(consent).model_dump(mode="json") if consent else None,
        "feedback": [
            {
                "prompt_key": row.prompt_key,
                "reason_code": row.reason_code,
                "comment": decrypt_restricted_comment(row.comment_encrypted),
                "route_template": row.route_template,
                "feature_key": row.feature_key,
                "created_at": row.created_at,
            }
            for row in feedback
        ],
        "events": [
            {
                "event_id": row.event_id,
                "event_name": row.event_name,
                "occurred_at": row.occurred_at,
                "source": row.source,
                "properties": row.properties,
            }
            for row in events
        ],
        "truncated": len(events) >= 10_000,
    }


@router.delete("/me", response_model=AnalyticsDeletionResponse, status_code=202)
def delete_my_analytics(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    request_row = request_analytics_deletion(
        db,
        user_id=current_user.id,
        distinct_id=str(current_user.id),
    )
    db.commit()
    db.refresh(request_row)
    enqueue_analytics_delivery_job()
    enqueue_analytics_privacy_job()
    return AnalyticsDeletionResponse(
        request_id=request_row.id,
        status=request_row.status,
        provider_status={
            key: value
            for key, value in (request_row.provider_status or {}).items()
            if isinstance(value, str)
        },
        requested_at=request_row.requested_at,
        completed_at=request_row.completed_at,
    )


@router.get("/me/deletions/{request_id}", response_model=AnalyticsDeletionResponse)
def analytics_deletion_status(
    request_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = db.get(AnalyticsDataRequest, request_id)
    deleted_distinct_id = (
        "deleted:" + hashlib.sha256(str(current_user.id).encode("utf-8")).hexdigest()
    )
    if row is None or (
        row.user_id != current_user.id and row.distinct_id != deleted_distinct_id
    ):
        raise HTTPException(status_code=404, detail="Deletion request not found")
    return AnalyticsDeletionResponse(
        request_id=row.id,
        status=row.status,
        provider_status={
            key: value
            for key, value in (row.provider_status or {}).items()
            if isinstance(value, str)
        },
        requested_at=row.requested_at,
        completed_at=row.completed_at,
    )


@router.get("/outbox/status")
def analytics_outbox_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    counts = dict(
        db.query(AnalyticsOutbox.delivery_status, func.count(AnalyticsOutbox.event_id))
        .group_by(AnalyticsOutbox.delivery_status)
        .all()
    )
    oldest = (
        db.query(func.min(AnalyticsOutbox.created_at))
        .filter(AnalyticsOutbox.delivery_status.in_(("pending", "failed")))
        .scalar()
    )
    return {"counts": counts, "oldest_pending_at": oldest}


@router.get("/quality")
def analytics_quality(
    window_hours: int = Query(default=24, ge=1, le=24 * 31),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return build_analytics_quality_report(db, window_hours=window_hours)
