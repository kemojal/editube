from __future__ import annotations

import json
import os
from dataclasses import dataclass


def _truthy(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class RequestLogSettings:
    enabled: bool
    write_database_url: str | None
    read_database_url: str | None
    encryption_key: str | None
    encryption_key_id: str | None
    decryption_keys: dict[str, str]
    hmac_key: str | None
    environment: str
    release: str | None
    request_body_limit: int
    response_body_limit: int
    queue_max_size: int
    batch_size: int
    flush_interval_ms: int
    success_payload_retention_days: int
    failure_payload_retention_days: int
    retention_interval_hours: int

    @classmethod
    def from_env(cls) -> "RequestLogSettings":
        write_url = (os.getenv("LOG_WRITE_DATABASE_URL") or "").strip() or None
        read_url = (os.getenv("LOG_READ_DATABASE_URL") or "").strip() or None
        enabled_raw = os.getenv("LOG_REQUESTS_ENABLED")
        enabled = _truthy(enabled_raw, default=bool(write_url))

        key = (os.getenv("LOG_PAYLOAD_ENCRYPTION_KEY") or "").strip() or None
        key_id = (os.getenv("LOG_PAYLOAD_ENCRYPTION_KEY_ID") or "").strip() or None
        hmac_key = (os.getenv("LOG_HMAC_KEY") or "").strip() or None
        old_keys_raw = (os.getenv("LOG_PAYLOAD_DECRYPTION_KEYS") or "").strip()
        old_keys: dict[str, str] = {}
        if old_keys_raw:
            try:
                parsed = json.loads(old_keys_raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError("LOG_PAYLOAD_DECRYPTION_KEYS must be a JSON object") from exc
            if not isinstance(parsed, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
            ):
                raise RuntimeError("LOG_PAYLOAD_DECRYPTION_KEYS must map key IDs to Fernet keys")
            old_keys = parsed
        if key and key_id:
            old_keys[key_id] = key

        settings = cls(
            enabled=enabled,
            write_database_url=write_url,
            read_database_url=read_url,
            encryption_key=key,
            encryption_key_id=key_id,
            decryption_keys=old_keys,
            hmac_key=hmac_key,
            environment=(os.getenv("APP_ENV") or "development").strip().lower(),
            release=(os.getenv("RELEASE") or "").strip() or None,
            request_body_limit=_bounded_int(
                "LOG_REQUEST_BODY_MAX_BYTES", 64 * 1024, 1024, 1024 * 1024
            ),
            response_body_limit=_bounded_int(
                "LOG_RESPONSE_BODY_MAX_BYTES", 128 * 1024, 1024, 2 * 1024 * 1024
            ),
            queue_max_size=_bounded_int("LOG_QUEUE_MAX_SIZE", 500, 10, 10_000),
            batch_size=_bounded_int("LOG_WRITE_BATCH_SIZE", 100, 1, 1000),
            flush_interval_ms=_bounded_int("LOG_WRITE_FLUSH_INTERVAL_MS", 500, 50, 10_000),
            success_payload_retention_days=_bounded_int(
                "LOG_SUCCESS_PAYLOAD_RETENTION_DAYS", 3, 1, 30
            ),
            failure_payload_retention_days=_bounded_int(
                "LOG_FAILURE_PAYLOAD_RETENTION_DAYS", 14, 1, 90
            ),
            retention_interval_hours=_bounded_int("LOG_RETENTION_INTERVAL_HOURS", 24, 1, 168),
        )
        if settings.enabled:
            settings.validate_for_capture()
        return settings

    def validate_for_capture(self) -> None:
        missing = [
            name
            for name, value in (
                ("LOG_WRITE_DATABASE_URL", self.write_database_url),
                ("LOG_PAYLOAD_ENCRYPTION_KEY", self.encryption_key),
                ("LOG_PAYLOAD_ENCRYPTION_KEY_ID", self.encryption_key_id),
                ("LOG_HMAC_KEY", self.hmac_key),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                "Request logging is enabled but required configuration is missing: "
                + ", ".join(missing)
            )
        if self.write_database_url and not self.write_database_url.startswith(
            ("postgresql://", "postgresql+psycopg2://", "postgres://")
        ):
            raise RuntimeError("LOG_WRITE_DATABASE_URL must be a PostgreSQL URL")

    def validate_crypto(self) -> None:
        missing = [
            name
            for name, value in (
                ("LOG_PAYLOAD_ENCRYPTION_KEY", self.encryption_key),
                ("LOG_PAYLOAD_ENCRYPTION_KEY_ID", self.encryption_key_id),
                ("LOG_HMAC_KEY", self.hmac_key),
            )
            if not value
        ]
        if missing:
            raise RuntimeError("Missing request-log cryptographic configuration: " + ", ".join(missing))

    def validate_for_read(self) -> None:
        if not self.read_database_url:
            raise RuntimeError("LOG_READ_DATABASE_URL is required for internal log access")
        if not self.decryption_keys:
            raise RuntimeError("No request-log decryption keys are configured")
        self.validate_crypto()
