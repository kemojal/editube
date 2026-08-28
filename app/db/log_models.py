from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, Boolean, Column, Date, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func
from sqlalchemy.sql.sqltypes import TIMESTAMP

from .database import Base


class ApiRequestLog(Base):
    __tablename__ = "api_requests"
    __table_args__ = (
        Index("ix_log_api_requests_occurred_at", "occurred_at"),
        Index("ix_log_api_requests_route_status", "route_template", "status_code"),
        Index("ix_log_api_requests_user_time", "user_id", "occurred_at"),
        Index("ix_log_api_requests_workspace_time", "workspace_id", "occurred_at"),
        {"schema": "log"},
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    request_id = Column(String(128), nullable=False, index=True)
    occurred_at = Column(TIMESTAMP(timezone=True), nullable=False)
    completed_at = Column(TIMESTAMP(timezone=True), nullable=False)
    environment = Column(String(32), nullable=False, index=True)
    release = Column(String(128), nullable=True)
    method = Column(String(16), nullable=False)
    route_template = Column(String(1024), nullable=True)
    endpoint_name = Column(String(256), nullable=True)
    status_code = Column(Integer, nullable=False)
    duration_ms = Column(Integer, nullable=False)
    request_size_bytes = Column(BigInteger, nullable=False, default=0)
    response_size_bytes = Column(BigInteger, nullable=False, default=0)
    request_content_type = Column(String(256), nullable=True)
    response_content_type = Column(String(256), nullable=True)
    request_headers = Column(JSONB, nullable=False, default=dict)
    response_headers = Column(JSONB, nullable=False, default=dict)
    client_ip_hash = Column(String(64), nullable=True)
    user_agent_hash = Column(String(64), nullable=True)
    user_id = Column(Integer, nullable=True)
    workspace_id = Column(Integer, nullable=True)
    trace_id = Column(String(64), nullable=True)
    capture_reason = Column(String(64), nullable=False)
    request_body_state = Column(String(32), nullable=False)
    response_body_state = Column(String(32), nullable=False)
    error_class = Column(String(128), nullable=True)
    payload_present = Column(Boolean, nullable=False, default=False)


class ApiPayloadLog(Base):
    __tablename__ = "api_payloads"
    __table_args__ = ({"schema": "log"},)

    request_log_id = Column(
        UUID(as_uuid=True),
        ForeignKey("log.api_requests.id", ondelete="CASCADE"),
        primary_key=True,
    )
    key_id = Column(String(128), nullable=False)
    algorithm = Column(String(32), nullable=False, default="fernet")
    request_ciphertext = Column(Text, nullable=True)
    request_sha256 = Column(String(64), nullable=True)
    response_ciphertext = Column(Text, nullable=True)
    response_sha256 = Column(String(64), nullable=True)
    redaction_summary = Column(JSONB, nullable=False, default=dict)
    expires_at = Column(TIMESTAMP(timezone=True), nullable=False, index=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())


class LogAccessEvent(Base):
    __tablename__ = "access_events"
    __table_args__ = (
        Index("ix_log_access_events_actor_time", "actor_user_id", "created_at"),
        {"schema": "log"},
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_user_id = Column(Integer, nullable=True)
    request_log_id = Column(UUID(as_uuid=True), nullable=True)
    action = Column(String(64), nullable=False)
    outcome = Column(String(32), nullable=False)
    reason = Column(Text, nullable=True)
    client_ip_hash = Column(String(64), nullable=True)
    user_agent_hash = Column(String(64), nullable=True)
    details = Column(JSONB, nullable=False, default=dict)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())


class LogAdminAccessGrant(Base):
    __tablename__ = "admin_access_grants"
    __table_args__ = (
        Index("ix_log_admin_grants_user_active", "user_id", "revoked_at", "expires_at"),
        {"schema": "log"},
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(Integer, nullable=False)
    granted_by_user_id = Column(Integer, nullable=False)
    can_read = Column(Boolean, nullable=False, default=True)
    can_decrypt = Column(Boolean, nullable=False, default=True)
    grant_reason = Column(Text, nullable=False)
    expires_at = Column(TIMESTAMP(timezone=True), nullable=True)
    revoked_at = Column(TIMESTAMP(timezone=True), nullable=True)
    revoked_by_user_id = Column(Integer, nullable=True)
    revoke_reason = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())


class LogRetentionPolicy(Base):
    __tablename__ = "retention_policy"
    __table_args__ = ({"schema": "log"},)

    singleton_id = Column(Boolean, primary_key=True, default=True)
    metadata_retention_days = Column(Integer, nullable=False, default=30)
    access_audit_retention_days = Column(Integer, nullable=False, default=400)
    rollup_retention_days = Column(Integer, nullable=False, default=400)
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())


class ApiRequestDailyRollup(Base):
    __tablename__ = "api_request_daily_rollups"
    __table_args__ = (
        UniqueConstraint(
            "day", "environment", "route_template", "method", "status_class",
            name="uq_log_daily_route_method_status",
        ),
        {"schema": "log"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    day = Column(Date, nullable=False, index=True)
    environment = Column(String(32), nullable=False)
    route_template = Column(String(1024), nullable=False)
    method = Column(String(16), nullable=False)
    status_class = Column(String(3), nullable=False)
    request_count = Column(BigInteger, nullable=False)
    failure_count = Column(BigInteger, nullable=False)
    average_duration_ms = Column(Float, nullable=False)
    p95_duration_ms = Column(Float, nullable=False)
    average_request_bytes = Column(Float, nullable=False)
    average_response_bytes = Column(Float, nullable=False)
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
