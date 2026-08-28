from __future__ import annotations

import base64
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app.db.database import Base
from app.db.log_models import LogAccessEvent, LogAdminAccessGrant
from app.db.models import User, UserMFAMethod
from app.request_logging.admin_access import authorize_log_access, issue_step_up_token
from app.services.mfa_totp import _hotp, clear_mfa_attempts
from app.utils.security import create_access_token


@pytest.fixture
def access_db(monkeypatch):  # noqa: ANN001
    monkeypatch.setenv("LOG_REQUESTS_ENABLED", "0")
    monkeypatch.setenv("LOG_READ_DATABASE_URL", "postgresql://reader:test@db.test/editube")
    monkeypatch.setenv("LOG_PAYLOAD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("LOG_PAYLOAD_ENCRYPTION_KEY_ID", "test-v1")
    monkeypatch.setenv(
        "LOG_HMAC_KEY", base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    )
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _attach_log_schema(connection, _record):  # noqa: ANN001
        connection.execute("ATTACH DATABASE ':memory:' AS log")

    Base.metadata.create_all(
        engine,
        tables=[
            User.__table__,
            UserMFAMethod.__table__,
            LogAdminAccessGrant.__table__,
            LogAccessEvent.__table__,
        ],
    )
    db = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _request(user_id: int, sid: str = "session-1") -> Request:
    access_token = create_access_token({"user_id": user_id, "sid": sid})
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/internal/request-logs",
            "headers": [
                (b"authorization", f"Bearer {access_token}".encode()),
                (b"user-agent", b"pytest"),
            ],
            "client": ("203.0.113.8", 443),
            "server": ("testserver", 443),
            "scheme": "https",
            "query_string": b"",
        }
    )


def _admin_with_grant(db, *, role: str = "admin", decrypt: bool = True):  # noqa: ANN001
    user = User(email=f"{uuid.uuid4()}@example.test", name="Internal", role=role)
    db.add(user)
    db.flush()
    secret = "JBSWY3DPEHPK3PXP"
    db.add(
        UserMFAMethod(
            user_id=user.id,
            method_type="totp",
            secret_encrypted=secret,
            verified_at=datetime.utcnow(),
        )
    )
    db.add(
        LogAdminAccessGrant(
            id=uuid.uuid4(),
            user_id=user.id,
            granted_by_user_id=user.id,
            can_read=True,
            can_decrypt=decrypt,
            grant_reason="Approved for incident response testing",
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
    )
    db.commit()
    return user, secret


def test_step_up_is_session_bound_and_access_is_audited(access_db):  # noqa: ANN001
    user, secret = _admin_with_grant(access_db)
    request = _request(user.id)
    code = _hotp(secret, int(time.time() // 30))
    token = issue_step_up_token(
        request=request,
        user=user,
        primary_db=access_db,
        log_db=access_db,
        code=code,
    )
    access = authorize_log_access(
        request=request,
        user=user,
        log_db=access_db,
        step_up_token=token,
        reason="Investigating request abc-123 failure",
        require_decrypt=True,
    )
    assert access.user.id == user.id
    assert access.grant.can_decrypt is True
    assert access_db.query(LogAccessEvent).filter(LogAccessEvent.action == "mfa_step_up").count() == 1

    with pytest.raises(HTTPException) as exc:
        authorize_log_access(
            request=_request(user.id, sid="different-session"),
            user=user,
            log_db=access_db,
            step_up_token=token,
            reason="Investigating request abc-123 failure",
            require_decrypt=True,
        )
    assert exc.value.status_code == 401


def test_workspace_role_cannot_gain_access_even_if_grant_row_exists(access_db):  # noqa: ANN001
    user, secret = _admin_with_grant(access_db, role="owner")
    with pytest.raises(HTTPException) as exc:
        issue_step_up_token(
            request=_request(user.id),
            user=user,
            primary_db=access_db,
            log_db=access_db,
            code=_hotp(secret, int(time.time() // 30)),
        )
    assert exc.value.status_code == 403
    denied = access_db.query(LogAccessEvent).filter(LogAccessEvent.outcome == "denied").one()
    assert denied.actor_user_id == user.id


def test_metadata_only_grant_cannot_decrypt(access_db):  # noqa: ANN001
    user, secret = _admin_with_grant(access_db, decrypt=False)
    request = _request(user.id)
    token = issue_step_up_token(
        request=request,
        user=user,
        primary_db=access_db,
        log_db=access_db,
        code=_hotp(secret, int(time.time() // 30)),
    )
    with pytest.raises(HTTPException) as exc:
        authorize_log_access(
            request=request,
            user=user,
            log_db=access_db,
            step_up_token=token,
            reason="Investigating a production API failure",
            require_decrypt=True,
        )
    assert exc.value.status_code == 403


def test_distributed_mfa_failure_budget_rate_limits_step_up(access_db):  # noqa: ANN001
    user, secret = _admin_with_grant(access_db)
    request = _request(user.id)
    valid_code = _hotp(secret, int(time.time() // 30))
    wrong_code = str((int(valid_code) + 1) % 1_000_000).zfill(6)
    attempt_key = f"request-log-step-up:{user.id}"
    clear_mfa_attempts(attempt_key)
    try:
        for _ in range(5):
            with pytest.raises(HTTPException) as exc:
                issue_step_up_token(
                    request=request,
                    user=user,
                    primary_db=access_db,
                    log_db=access_db,
                    code=wrong_code,
                )
            assert exc.value.status_code == 401

        with pytest.raises(HTTPException) as exc:
            issue_step_up_token(
                request=request,
                user=user,
                primary_db=access_db,
                log_db=access_db,
                code=valid_code,
            )
        assert exc.value.status_code == 429
        outcomes = [
            row.outcome
            for row in access_db.query(LogAccessEvent)
            .filter(LogAccessEvent.action == "mfa_step_up")
            .order_by(LogAccessEvent.created_at)
            .all()
        ]
        assert outcomes.count("denied") == 5
        assert outcomes.count("rate_limited") == 1
    finally:
        clear_mfa_attempts(attempt_key)
