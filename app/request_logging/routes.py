from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.log_models import ApiPayloadLog, ApiRequestDailyRollup, ApiRequestLog, LogAccessEvent
from app.db.models import User
from app.request_logging.admin_access import (
    authorize_log_access,
    issue_step_up_token,
    record_log_access,
)
from app.request_logging.config import RequestLogSettings
from app.request_logging.crypto import PayloadCipher, REDACTED
from app.request_logging.database import get_log_read_db
from app.utils.security import get_current_user

from .schemas import LogMFAStepUpRequest


router = APIRouter(prefix="/internal/request-logs", tags=["Internal Request Logs"])


_ANALYTICS_MAX_WINDOWS = {
    "minute": timedelta(hours=24),
    "hour": timedelta(days=31),
    "day": timedelta(days=31),
}


def _validate_analytics_window(bucket: str, from_ts: datetime, to_ts: datetime) -> None:
    if bucket not in _ANALYTICS_MAX_WINDOWS:
        raise HTTPException(status_code=422, detail="Unsupported analytics bucket")
    if from_ts.tzinfo is None or to_ts.tzinfo is None:
        raise HTTPException(status_code=422, detail="Analytics timestamps must include a timezone")
    if from_ts >= to_ts:
        raise HTTPException(status_code=422, detail="from_ts must be earlier than to_ts")
    max_window = _ANALYTICS_MAX_WINDOWS[bucket]
    if to_ts - from_ts > max_window:
        raise HTTPException(
            status_code=422,
            detail=f"The maximum {bucket} analytics window is {max_window}",
        )


def _apply_request_filters(
    query,
    *,
    from_ts: datetime,
    to_ts: datetime,
    environment: str | None,
    method: str | None,
    route: str | None,
):
    query = query.filter(
        ApiRequestLog.occurred_at >= from_ts,
        ApiRequestLog.occurred_at < to_ts,
    )
    if environment:
        query = query.filter(ApiRequestLog.environment == environment)
    if method:
        query = query.filter(ApiRequestLog.method == method.upper())
    if route:
        query = query.filter(ApiRequestLog.route_template.ilike(f"%{route}%"))
    return query


def _serialize_request(row: ApiRequestLog) -> dict:
    return {
        "id": str(row.id),
        "request_id": row.request_id,
        "occurred_at": row.occurred_at,
        "completed_at": row.completed_at,
        "environment": row.environment,
        "release": row.release,
        "method": row.method,
        "route_template": row.route_template,
        "endpoint_name": row.endpoint_name,
        "status_code": row.status_code,
        "duration_ms": row.duration_ms,
        "request_size_bytes": row.request_size_bytes,
        "response_size_bytes": row.response_size_bytes,
        "request_content_type": row.request_content_type,
        "response_content_type": row.response_content_type,
        "request_headers": row.request_headers,
        "response_headers": row.response_headers,
        "client_ip_hash": row.client_ip_hash,
        "user_agent_hash": row.user_agent_hash,
        "user_id": row.user_id,
        "workspace_id": row.workspace_id,
        "trace_id": row.trace_id,
        "capture_reason": row.capture_reason,
        "request_body_state": row.request_body_state,
        "response_body_state": row.response_body_state,
        "error_class": row.error_class,
        "payload_present": row.payload_present,
    }


def _authorize(
    request: Request,
    current_user: User,
    log_db: Session,
    step_up_token: str | None,
    reason: str | None,
    *,
    decrypt: bool,
):
    return authorize_log_access(
        request=request,
        user=current_user,
        log_db=log_db,
        step_up_token=step_up_token,
        reason=reason,
        require_decrypt=decrypt,
    )


@router.post("/mfa-step-up")
def request_log_mfa_step_up(
    body: LogMFAStepUpRequest,
    request: Request,
    db: Session = Depends(get_db),
    log_db: Session = Depends(get_log_read_db),
    current_user: User = Depends(get_current_user),
):
    token = issue_step_up_token(
        request=request,
        user=current_user,
        primary_db=db,
        log_db=log_db,
        code=body.code,
    )
    return {"step_up_token": token, "token_type": "bearer", "expires_in": 300}


@router.get("/health")
def request_log_health(
    request: Request,
    x_log_step_up_token: str | None = Header(default=None, alias="X-Log-Step-Up-Token"),
    x_log_access_reason: str | None = Header(default=None, alias="X-Log-Access-Reason"),
    log_db: Session = Depends(get_log_read_db),
    current_user: User = Depends(get_current_user),
):
    access = _authorize(
        request, current_user, log_db, x_log_step_up_token, x_log_access_reason, decrypt=False
    )
    latest_request_at = log_db.query(func.max(ApiRequestLog.occurred_at)).scalar()
    health = {
        "database_readable": True,
        "latest_request_at": latest_request_at,
        "active_key_id": RequestLogSettings.from_env().encryption_key_id,
    }
    record_log_access(
        log_db,
        request,
        actor_user_id=current_user.id,
        action="health",
        outcome="success",
        reason=access.reason,
    )
    return health


@router.get("/analytics/daily")
def request_log_daily_analytics(
    request: Request,
    from_day: datetime | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
    x_log_step_up_token: str | None = Header(default=None, alias="X-Log-Step-Up-Token"),
    x_log_access_reason: str | None = Header(default=None, alias="X-Log-Access-Reason"),
    log_db: Session = Depends(get_log_read_db),
    current_user: User = Depends(get_current_user),
):
    access = _authorize(
        request, current_user, log_db, x_log_step_up_token, x_log_access_reason, decrypt=False
    )
    query = log_db.query(ApiRequestDailyRollup)
    if from_day:
        query = query.filter(ApiRequestDailyRollup.day >= from_day.date())
    rows = query.order_by(ApiRequestDailyRollup.day.desc()).limit(limit).all()
    result = [
        {
            "day": row.day,
            "environment": row.environment,
            "route_template": row.route_template,
            "method": row.method,
            "status_class": row.status_class,
            "request_count": row.request_count,
            "failure_count": row.failure_count,
            "average_duration_ms": row.average_duration_ms,
            "p95_duration_ms": row.p95_duration_ms,
            "average_request_bytes": row.average_request_bytes,
            "average_response_bytes": row.average_response_bytes,
        }
        for row in rows
    ]
    record_log_access(
        log_db,
        request,
        actor_user_id=current_user.id,
        action="analytics",
        outcome="success",
        reason=access.reason,
        details={"rows": len(result)},
    )
    return {"items": result}


@router.get("/analytics/timeline")
def request_log_timeline_analytics(
    request: Request,
    from_ts: datetime = Query(...),
    to_ts: datetime = Query(...),
    bucket: str = Query(default="hour", pattern="^(minute|hour|day)$"),
    environment: str | None = Query(default=None, max_length=32),
    method: str | None = Query(default=None, max_length=16),
    route: str | None = Query(default=None, max_length=256),
    top_route_limit: int = Query(default=8, ge=1, le=25),
    x_log_step_up_token: str | None = Header(default=None, alias="X-Log-Step-Up-Token"),
    x_log_access_reason: str | None = Header(default=None, alias="X-Log-Access-Reason"),
    log_db: Session = Depends(get_log_read_db),
    current_user: User = Depends(get_current_user),
):
    """Return bounded, live analytics from retained request metadata.

    Raw metadata is intentionally used here so the current minute/hour/day is
    visible before the daily retention job creates rollups. Longer historical
    analysis belongs to the daily rollup endpoint.
    """
    access = _authorize(
        request, current_user, log_db, x_log_step_up_token, x_log_access_reason, decrypt=False
    )
    _validate_analytics_window(bucket, from_ts, to_ts)

    bucket_start = func.date_trunc(bucket, ApiRequestLog.occurred_at).label("bucket_start")
    failure_count = func.sum(
        case((ApiRequestLog.status_code >= 400, 1), else_=0)
    ).label("failure_count")

    timeline_query = log_db.query(
        bucket_start,
        func.count(ApiRequestLog.id).label("request_count"),
        failure_count,
        func.avg(ApiRequestLog.duration_ms).label("average_duration_ms"),
        func.percentile_cont(0.95)
        .within_group(ApiRequestLog.duration_ms)
        .label("p95_duration_ms"),
        func.sum(case((ApiRequestLog.status_code.between(200, 299), 1), else_=0)).label(
            "status_2xx"
        ),
        func.sum(case((ApiRequestLog.status_code.between(300, 399), 1), else_=0)).label(
            "status_3xx"
        ),
        func.sum(case((ApiRequestLog.status_code.between(400, 499), 1), else_=0)).label(
            "status_4xx"
        ),
        func.sum(case((ApiRequestLog.status_code.between(500, 599), 1), else_=0)).label(
            "status_5xx"
        ),
    )
    timeline_query = _apply_request_filters(
        timeline_query,
        from_ts=from_ts,
        to_ts=to_ts,
        environment=environment,
        method=method,
        route=route,
    )
    timeline_rows = timeline_query.group_by(bucket_start).order_by(bucket_start).all()

    summary_query = log_db.query(
        func.count(ApiRequestLog.id).label("request_count"),
        failure_count,
        func.avg(ApiRequestLog.duration_ms).label("average_duration_ms"),
        func.percentile_cont(0.95)
        .within_group(ApiRequestLog.duration_ms)
        .label("p95_duration_ms"),
        func.sum(ApiRequestLog.request_size_bytes).label("request_bytes"),
        func.sum(ApiRequestLog.response_size_bytes).label("response_bytes"),
    )
    summary = _apply_request_filters(
        summary_query,
        from_ts=from_ts,
        to_ts=to_ts,
        environment=environment,
        method=method,
        route=route,
    ).one()

    route_name = func.coalesce(ApiRequestLog.route_template, "<unmatched>").label("route")
    top_routes_query = log_db.query(
        route_name,
        ApiRequestLog.method,
        func.count(ApiRequestLog.id).label("request_count"),
        failure_count,
        func.avg(ApiRequestLog.duration_ms).label("average_duration_ms"),
    )
    top_routes = (
        _apply_request_filters(
            top_routes_query,
            from_ts=from_ts,
            to_ts=to_ts,
            environment=environment,
            method=method,
            route=route,
        )
        .group_by(route_name, ApiRequestLog.method)
        .order_by(func.count(ApiRequestLog.id).desc())
        .limit(top_route_limit)
        .all()
    )

    total = int(summary.request_count or 0)
    failures = int(summary.failure_count or 0)
    result = {
        "bucket": bucket,
        "from_ts": from_ts,
        "to_ts": to_ts,
        "summary": {
            "request_count": total,
            "failure_count": failures,
            "error_rate": failures / total if total else 0.0,
            "average_duration_ms": float(summary.average_duration_ms or 0),
            "p95_duration_ms": float(summary.p95_duration_ms or 0),
            "request_bytes": int(summary.request_bytes or 0),
            "response_bytes": int(summary.response_bytes or 0),
        },
        "buckets": [
            {
                "start": row.bucket_start,
                "request_count": int(row.request_count or 0),
                "failure_count": int(row.failure_count or 0),
                "average_duration_ms": float(row.average_duration_ms or 0),
                "p95_duration_ms": float(row.p95_duration_ms or 0),
                "status_2xx": int(row.status_2xx or 0),
                "status_3xx": int(row.status_3xx or 0),
                "status_4xx": int(row.status_4xx or 0),
                "status_5xx": int(row.status_5xx or 0),
            }
            for row in timeline_rows
        ],
        "top_routes": [
            {
                "route": row.route,
                "method": row.method,
                "request_count": int(row.request_count or 0),
                "failure_count": int(row.failure_count or 0),
                "average_duration_ms": float(row.average_duration_ms or 0),
            }
            for row in top_routes
        ],
    }
    record_log_access(
        log_db,
        request,
        actor_user_id=current_user.id,
        action="analytics_timeline",
        outcome="success",
        reason=access.reason,
        details={
            "bucket": bucket,
            "rows": len(result["buckets"]),
            "environment": environment,
            "method": method.upper() if method else None,
            "route_filter_present": bool(route),
        },
    )
    return result


@router.get("/access-events")
def list_request_log_access_events(
    request: Request,
    actor_user_id: int | None = Query(default=None),
    action: str | None = Query(default=None, max_length=64),
    outcome: str | None = Query(default=None, max_length=32),
    from_ts: datetime | None = Query(default=None),
    to_ts: datetime | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    x_log_step_up_token: str | None = Header(default=None, alias="X-Log-Step-Up-Token"),
    x_log_access_reason: str | None = Header(default=None, alias="X-Log-Access-Reason"),
    log_db: Session = Depends(get_log_read_db),
    current_user: User = Depends(get_current_user),
):
    access = _authorize(
        request, current_user, log_db, x_log_step_up_token, x_log_access_reason, decrypt=False
    )
    query = log_db.query(LogAccessEvent)
    if actor_user_id is not None:
        query = query.filter(LogAccessEvent.actor_user_id == actor_user_id)
    if action:
        query = query.filter(LogAccessEvent.action == action)
    if outcome:
        query = query.filter(LogAccessEvent.outcome == outcome)
    if from_ts:
        query = query.filter(LogAccessEvent.created_at >= from_ts)
    if to_ts:
        query = query.filter(LogAccessEvent.created_at <= to_ts)
    rows = query.order_by(LogAccessEvent.created_at.desc()).limit(limit).all()
    items = [
        {
            "id": str(row.id),
            "actor_user_id": row.actor_user_id,
            "request_log_id": str(row.request_log_id) if row.request_log_id else None,
            "action": row.action,
            "outcome": row.outcome,
            "reason": row.reason,
            "client_ip_hash": row.client_ip_hash,
            "user_agent_hash": row.user_agent_hash,
            "details": row.details,
            "created_at": row.created_at,
        }
        for row in rows
    ]
    record_log_access(
        log_db,
        request,
        actor_user_id=current_user.id,
        action="view_access_audit",
        outcome="success",
        reason=access.reason,
        details={"rows": len(items)},
    )
    return {"items": items}


@router.get("")
def search_request_logs(
    request: Request,
    request_id: str | None = Query(default=None, max_length=128),
    method: str | None = Query(default=None, max_length=16),
    route: str | None = Query(default=None, max_length=256),
    environment: str | None = Query(default=None, max_length=32),
    status_code: int | None = Query(default=None, ge=100, le=599),
    only_errors: bool = Query(default=False),
    user_id: int | None = Query(default=None),
    workspace_id: int | None = Query(default=None),
    from_ts: datetime | None = Query(default=None),
    to_ts: datetime | None = Query(default=None),
    offset: int = Query(default=0, ge=0, le=100_000),
    limit: int = Query(default=100, ge=1, le=500),
    x_log_step_up_token: str | None = Header(default=None, alias="X-Log-Step-Up-Token"),
    x_log_access_reason: str | None = Header(default=None, alias="X-Log-Access-Reason"),
    log_db: Session = Depends(get_log_read_db),
    current_user: User = Depends(get_current_user),
):
    access = _authorize(
        request, current_user, log_db, x_log_step_up_token, x_log_access_reason, decrypt=False
    )
    query = log_db.query(ApiRequestLog)
    if request_id:
        query = query.filter(ApiRequestLog.request_id == request_id)
    if method:
        query = query.filter(ApiRequestLog.method == method.upper())
    if route:
        query = query.filter(ApiRequestLog.route_template.ilike(f"%{route}%"))
    if environment:
        query = query.filter(ApiRequestLog.environment == environment)
    if status_code is not None:
        query = query.filter(ApiRequestLog.status_code == status_code)
    if only_errors:
        query = query.filter(ApiRequestLog.status_code >= 400)
    if user_id is not None:
        query = query.filter(ApiRequestLog.user_id == user_id)
    if workspace_id is not None:
        query = query.filter(ApiRequestLog.workspace_id == workspace_id)
    if from_ts:
        query = query.filter(ApiRequestLog.occurred_at >= from_ts)
    if to_ts:
        query = query.filter(ApiRequestLog.occurred_at <= to_ts)
    rows = (
        query.order_by(ApiRequestLog.occurred_at.desc())
        .offset(offset)
        .limit(limit + 1)
        .all()
    )
    has_more = len(rows) > limit
    items = [_serialize_request(row) for row in rows[:limit]]
    record_log_access(
        log_db,
        request,
        actor_user_id=current_user.id,
        action="search",
        outcome="success",
        reason=access.reason,
        details={"returned": len(items), "offset": offset, "filters_applied": True},
    )
    return {"items": items, "offset": offset, "limit": limit, "has_more": has_more}


def _load_request_and_payload(log_db: Session, log_id: uuid.UUID):
    row = log_db.query(ApiRequestLog).filter(ApiRequestLog.id == log_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Request log not found")
    payload = (
        log_db.query(ApiPayloadLog)
        .filter(ApiPayloadLog.request_log_id == log_id)
        .first()
    )
    return row, payload


def _decrypt_payload(payload: ApiPayloadLog | None) -> tuple[dict | None, dict | None]:
    if payload is None:
        return None, None
    settings = RequestLogSettings.from_env()
    settings.validate_for_read()
    cipher = PayloadCipher(settings)
    request_data = (
        cipher.decrypt(payload.request_ciphertext, payload.key_id)
        if payload.request_ciphertext
        else None
    )
    response_data = (
        cipher.decrypt(payload.response_ciphertext, payload.key_id)
        if payload.response_ciphertext
        else None
    )
    return request_data, response_data


@router.get("/{log_id}/payload")
def decrypt_request_log_payload(
    log_id: uuid.UUID,
    request: Request,
    x_log_step_up_token: str | None = Header(default=None, alias="X-Log-Step-Up-Token"),
    x_log_access_reason: str | None = Header(default=None, alias="X-Log-Access-Reason"),
    log_db: Session = Depends(get_log_read_db),
    current_user: User = Depends(get_current_user),
):
    access = _authorize(
        request, current_user, log_db, x_log_step_up_token, x_log_access_reason, decrypt=True
    )
    row, payload = _load_request_and_payload(log_db, log_id)
    try:
        request_data, response_data = _decrypt_payload(payload)
    except Exception as exc:  # noqa: BLE001
        record_log_access(
            log_db,
            request,
            actor_user_id=current_user.id,
            request_log_id=log_id,
            action="decrypt",
            outcome="failure",
            reason=access.reason,
            details={"error_class": type(exc).__name__},
        )
        raise HTTPException(status_code=503, detail="Stored payload could not be decrypted") from exc
    record_log_access(
        log_db,
        request,
        actor_user_id=current_user.id,
        request_log_id=log_id,
        action="decrypt",
        outcome="success",
        reason=access.reason,
        details={"key_id": payload.key_id if payload else None},
    )
    return {
        "request": _serialize_request(row),
        "request_payload": request_data,
        "response_payload": response_data,
        "redaction_summary": payload.redaction_summary if payload else None,
        "payload_expires_at": payload.expires_at if payload else None,
    }


@router.get("/{log_id}/reproduction")
def build_reproduction_manifest(
    log_id: uuid.UUID,
    request: Request,
    x_log_step_up_token: str | None = Header(default=None, alias="X-Log-Step-Up-Token"),
    x_log_access_reason: str | None = Header(default=None, alias="X-Log-Access-Reason"),
    log_db: Session = Depends(get_log_read_db),
    current_user: User = Depends(get_current_user),
):
    access = _authorize(
        request, current_user, log_db, x_log_step_up_token, x_log_access_reason, decrypt=True
    )
    row, payload = _load_request_and_payload(log_db, log_id)
    try:
        request_data, _ = _decrypt_payload(payload)
    except Exception as exc:  # noqa: BLE001
        record_log_access(
            log_db,
            request,
            actor_user_id=current_user.id,
            request_log_id=log_id,
            action="build_reproduction",
            outcome="failure",
            reason=access.reason,
            details={"error_class": type(exc).__name__},
        )
        raise HTTPException(status_code=503, detail="Stored payload could not be decrypted") from exc
    if request_data is None:
        record_log_access(
            log_db,
            request,
            actor_user_id=current_user.id,
            request_log_id=log_id,
            action="build_reproduction",
            outcome="unavailable",
            reason=access.reason,
        )
        raise HTTPException(status_code=409, detail="No retained request payload is available")
    headers = dict(row.request_headers or {})
    headers.pop("cookie", None)
    headers.pop("content-length", None)
    headers["authorization"] = "Bearer <REPLACE_WITH_TEST_CREDENTIAL>"
    safe_method = row.method in {"GET", "HEAD"}
    manifest = {
        "method": row.method,
        "path": request_data.get("path"),
        "query": request_data.get("query"),
        "headers": headers,
        "body": request_data.get("body"),
        "automatic_production_replay_allowed": safe_method,
        "warning": (
            None
            if safe_method
            else "Mutating requests must be reproduced in a non-production environment or through an endpoint-specific side-effect-controlled adapter."
        ),
        "credentials": REDACTED,
    }
    record_log_access(
        log_db,
        request,
        actor_user_id=current_user.id,
        request_log_id=log_id,
        action="build_reproduction",
        outcome="success",
        reason=access.reason,
        details={"method": row.method, "production_safe_method": safe_method},
    )
    return manifest
