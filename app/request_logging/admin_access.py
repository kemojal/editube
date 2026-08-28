from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request
from jose import JWTError, jwt
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.db.log_models import LogAccessEvent, LogAdminAccessGrant
from app.db.models import User, UserMFAMethod
from app.services.mfa_totp import (
    clear_mfa_attempts,
    mfa_attempts_remaining,
    record_failed_mfa_attempt,
    verify_totp_code,
)
from app.utils.security import ALGORITHM, SECRET_KEY, create_access_token, decode_access_token_payload

from .config import RequestLogSettings
from .crypto import PayloadCipher


INTERNAL_ROLES = {"admin", "internal_admin", "super_admin"}
STEP_UP_MINUTES = 5


@dataclass(frozen=True)
class AuthorizedLogAccess:
    user: User
    grant: LogAdminAccessGrant
    reason: str


def _active_grant(log_db: Session, user_id: int) -> LogAdminAccessGrant | None:
    return (
        log_db.query(LogAdminAccessGrant)
        .filter(
            LogAdminAccessGrant.user_id == user_id,
            LogAdminAccessGrant.revoked_at.is_(None),
            or_(
                LogAdminAccessGrant.expires_at.is_(None),
                LogAdminAccessGrant.expires_at > datetime.now(timezone.utc),
            ),
        )
        .order_by(LogAdminAccessGrant.created_at.desc())
        .first()
    )


def _bearer_payload(request: Request) -> dict:
    header = (request.headers.get("authorization") or "").strip()
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Interactive bearer session required")
    token = header.split(" ", 1)[1].strip()
    if token.startswith("edt_"):
        raise HTTPException(status_code=403, detail="API tokens cannot access request logs")
    return decode_access_token_payload(token)


def _audit_identity(request: Request) -> tuple[str | None, str | None]:
    settings = RequestLogSettings.from_env()
    cipher = PayloadCipher(settings)
    client = request.client.host if request.client else None
    return cipher.keyed_hash(client), cipher.keyed_hash(request.headers.get("user-agent"))


def record_log_access(
    log_db: Session,
    request: Request,
    *,
    actor_user_id: int | None,
    action: str,
    outcome: str,
    reason: str | None,
    request_log_id: uuid.UUID | None = None,
    details: dict | None = None,
) -> None:
    ip_hash, user_agent_hash = _audit_identity(request)
    log_db.add(
        LogAccessEvent(
            actor_user_id=actor_user_id,
            request_log_id=request_log_id,
            action=action,
            outcome=outcome,
            reason=reason,
            client_ip_hash=ip_hash,
            user_agent_hash=user_agent_hash,
            details=details or {},
        )
    )
    # Access must fail closed if its audit event cannot be made durable.
    try:
        log_db.commit()
    except Exception as exc:  # noqa: BLE001
        log_db.rollback()
        raise HTTPException(status_code=503, detail="Request-log access audit unavailable") from exc


def issue_step_up_token(
    *,
    request: Request,
    user: User,
    primary_db: Session,
    log_db: Session,
    code: str,
) -> str:
    grant = _active_grant(log_db, user.id)
    if user.role not in INTERNAL_ROLES or not grant or not grant.can_read:
        record_log_access(
            log_db,
            request,
            actor_user_id=user.id,
            action="mfa_step_up",
            outcome="denied",
            reason="No active internal request-log grant",
        )
        raise HTTPException(status_code=403, detail="Request-log access is not granted")
    try:
        bearer = _bearer_payload(request)
    except HTTPException:
        record_log_access(
            log_db,
            request,
            actor_user_id=user.id,
            action="mfa_step_up",
            outcome="denied",
            reason="Interactive session required",
        )
        raise
    method = (
        primary_db.query(UserMFAMethod)
        .filter(
            UserMFAMethod.user_id == user.id,
            UserMFAMethod.verified_at.isnot(None),
            UserMFAMethod.disabled_at.is_(None),
        )
        .order_by(UserMFAMethod.id.desc())
        .first()
    )
    if not method:
        record_log_access(
            log_db,
            request,
            actor_user_id=user.id,
            action="mfa_step_up",
            outcome="denied",
            reason="No verified MFA method",
        )
        raise HTTPException(status_code=403, detail="Verified MFA is required")

    attempt_key = f"request-log-step-up:{user.id}"
    recent_failures = (
        log_db.query(LogAccessEvent)
        .filter(
            LogAccessEvent.actor_user_id == user.id,
            LogAccessEvent.action == "mfa_step_up",
            LogAccessEvent.outcome.in_(("denied", "rate_limited")),
            LogAccessEvent.created_at >= datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        .count()
    )
    if recent_failures >= 5 or mfa_attempts_remaining(attempt_key) <= 0:
        record_log_access(
            log_db,
            request,
            actor_user_id=user.id,
            action="mfa_step_up",
            outcome="rate_limited",
            reason="MFA attempt budget exhausted",
        )
        raise HTTPException(status_code=429, detail="Too many incorrect MFA codes")
    if not verify_totp_code(method.secret_encrypted, code):
        remaining = record_failed_mfa_attempt(attempt_key)
        record_log_access(
            log_db,
            request,
            actor_user_id=user.id,
            action="mfa_step_up",
            outcome="denied",
            reason="Incorrect MFA code",
            details={"attempts_remaining": remaining},
        )
        raise HTTPException(status_code=401, detail="Invalid MFA code")

    clear_mfa_attempts(attempt_key)
    sid = bearer.get("sid")
    if not sid:
        raise HTTPException(status_code=403, detail="Session-bound authentication required")
    token = create_access_token(
        {
            "user_id": user.id,
            "sid": sid,
            "purpose": "request_log_step_up",
            "scope": "request_logs",
            "jti": str(uuid.uuid4()),
        },
        expires_minutes=STEP_UP_MINUTES,
    )
    record_log_access(
        log_db,
        request,
        actor_user_id=user.id,
        action="mfa_step_up",
        outcome="success",
        reason="Interactive MFA verification",
        details={"valid_for_seconds": STEP_UP_MINUTES * 60},
    )
    return token


def authorize_log_access(
    *,
    request: Request,
    user: User,
    log_db: Session,
    step_up_token: str | None,
    reason: str | None,
    require_decrypt: bool,
) -> AuthorizedLogAccess:
    clean_reason = (reason or "").strip()
    if len(clean_reason) < 10 or len(clean_reason) > 1000:
        record_log_access(
            log_db,
            request,
            actor_user_id=user.id,
            action="authorize",
            outcome="denied",
            reason="Invalid or missing access reason",
        )
        raise HTTPException(
            status_code=400,
            detail="X-Log-Access-Reason must be between 10 and 1000 characters",
        )
    grant = _active_grant(log_db, user.id)
    if user.role not in INTERNAL_ROLES or not grant or not grant.can_read:
        record_log_access(
            log_db,
            request,
            actor_user_id=user.id,
            action="authorize",
            outcome="denied",
            reason=clean_reason,
            details={"decrypt_requested": require_decrypt},
        )
        raise HTTPException(status_code=403, detail="Request-log access is not granted")
    if require_decrypt and not grant.can_decrypt:
        record_log_access(
            log_db,
            request,
            actor_user_id=user.id,
            action="authorize_decrypt",
            outcome="denied",
            reason=clean_reason,
        )
        raise HTTPException(status_code=403, detail="Payload decryption is not granted")
    if not step_up_token:
        record_log_access(
            log_db,
            request,
            actor_user_id=user.id,
            action="authorize",
            outcome="denied",
            reason=clean_reason,
            details={"cause": "missing_step_up"},
        )
        raise HTTPException(status_code=401, detail="Recent request-log MFA step-up required")
    try:
        payload = jwt.decode(step_up_token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError as exc:
        record_log_access(
            log_db,
            request,
            actor_user_id=user.id,
            action="authorize",
            outcome="denied",
            reason=clean_reason,
            details={"cause": "invalid_step_up"},
        )
        raise HTTPException(status_code=401, detail="Invalid or expired MFA step-up token") from exc
    try:
        token_user_id = int(payload.get("user_id", -1))
        bearer = _bearer_payload(request)
    except (TypeError, ValueError, HTTPException):
        record_log_access(
            log_db,
            request,
            actor_user_id=user.id,
            action="authorize",
            outcome="denied",
            reason=clean_reason,
            details={"cause": "session_validation_failed"},
        )
        raise HTTPException(status_code=401, detail="MFA step-up token does not match this session")
    if (
        payload.get("purpose") != "request_log_step_up"
        or payload.get("scope") != "request_logs"
        or token_user_id != user.id
        or not payload.get("sid")
        or payload.get("sid") != bearer.get("sid")
    ):
        record_log_access(
            log_db,
            request,
            actor_user_id=user.id,
            action="authorize",
            outcome="denied",
            reason=clean_reason,
            details={"cause": "session_mismatch"},
        )
        raise HTTPException(status_code=401, detail="MFA step-up token does not match this session")
    return AuthorizedLogAccess(user=user, grant=grant, reason=clean_reason)
