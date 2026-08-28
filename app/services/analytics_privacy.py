"""Fail-closed validation for analytics properties and restricted feedback."""

from __future__ import annotations

import os
import re
from typing import Any

from cryptography.fernet import Fernet


class AnalyticsPrivacyError(ValueError):
    pass


_PROHIBITED_KEY_PARTS = (
    "email",
    "phone",
    "password",
    "authorization",
    "cookie",
    "secret",
    "token",
    "signed_url",
    "file_path",
    "full_name",
    "project_name",
    "workspace_name",
    "video_name",
    "video_title",
    "clip_title",
    "transcript",
    "prompt_text",
    "comment_text",
    "message_text",
    "contract_text",
    "invoice_text",
    "ip_address",
    "user_agent",
)
_PROHIBITED_KEYS = {
    "ip",
    "url",
    "href",
    "src",
    "address",
    "request_body",
    "response_body",
}
_EMAIL_RE = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_SECRET_RE = re.compile(
    r"(?i)(?:bearer\s+[a-z0-9._~+/=-]+|sk_(?:live|test)_[a-z0-9]+|rk_(?:live|test)_[a-z0-9]+|edt_[a-f0-9]{16,})"
)
_SAFE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")


def _validate_value(value: Any, *, path: str, depth: int) -> Any:
    if depth > 4:
        raise AnalyticsPrivacyError(f"Analytics property nesting is too deep at {path}")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > 500:
            raise AnalyticsPrivacyError(f"Analytics string is too long at {path}")
        if _EMAIL_RE.search(value) or _SECRET_RE.search(value):
            raise AnalyticsPrivacyError(f"Analytics value contains prohibited data at {path}")
        if "://" in value:
            raise AnalyticsPrivacyError(f"Full URLs are prohibited in analytics at {path}")
        return value
    if isinstance(value, (list, tuple)):
        if len(value) > 100:
            raise AnalyticsPrivacyError(f"Analytics array is too large at {path}")
        return [
            _validate_value(item, path=f"{path}[{index}]", depth=depth + 1)
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        return sanitize_properties(value, _path=path, _depth=depth + 1)
    raise AnalyticsPrivacyError(f"Unsupported analytics property type at {path}")


def sanitize_properties(
    properties: dict[str, Any] | None,
    *,
    _path: str = "properties",
    _depth: int = 0,
) -> dict[str, Any]:
    if not properties:
        return {}
    if len(properties) > 80:
        raise AnalyticsPrivacyError("Analytics event has too many properties")

    clean: dict[str, Any] = {}
    for raw_key, value in properties.items():
        key = str(raw_key).strip().lower()
        if not _SAFE_KEY_RE.fullmatch(key):
            raise AnalyticsPrivacyError(f"Invalid analytics property key: {raw_key}")
        if key in _PROHIBITED_KEYS or any(part in key for part in _PROHIBITED_KEY_PARTS):
            raise AnalyticsPrivacyError(f"Prohibited analytics property key: {key}")
        clean[key] = _validate_value(value, path=f"{_path}.{key}", depth=_depth)
    return clean


def encrypt_restricted_comment(comment: str | None) -> str | None:
    value = (comment or "").strip()
    if not value:
        return None
    if len(value) > 2000:
        raise AnalyticsPrivacyError("Feedback comment is too long")
    key = (
        os.getenv("ANALYTICS_FEEDBACK_ENCRYPTION_KEY", "").strip()
        or os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()
    )
    if not key:
        raise AnalyticsPrivacyError(
            "Encrypted feedback storage is not configured; omit the free-text comment"
        )
    return Fernet(key.encode("utf-8")).encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_restricted_comment(ciphertext: str | None) -> str | None:
    if not ciphertext:
        return None
    key = (
        os.getenv("ANALYTICS_FEEDBACK_ENCRYPTION_KEY", "").strip()
        or os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()
    )
    if not key:
        return None
    return Fernet(key.encode("utf-8")).decrypt(ciphertext.encode("utf-8")).decode("utf-8")
