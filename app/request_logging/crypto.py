from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode

from cryptography.fernet import Fernet, InvalidToken

from .config import RequestLogSettings


REDACTED = "[REDACTED]"
_SENSITIVE_KEY_RE = re.compile(
    r"(^|[_-])(authorization|auth|cookie|password|passwd|secret|token|api[_-]?key|"
    r"session|credential|signature|client[_-]?secret|private[_-]?key|recovery[_-]?code|"
    r"totp|otp|code|card|cvc|cvv|ssn|tax[_-]?id|bank[_-]?account|routing[_-]?number|"
    r"iban|date[_-]?of[_-]?birth|dob)($|[_-])",
    re.IGNORECASE,
)
_SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "set-cookie",
    "proxy-authorization",
    "x-api-key",
    "x-auth-token",
    "x-csrf-token",
    "x-forwarded-for",
    "x-real-ip",
    "cf-connecting-ip",
    "stripe-signature",
    "referer",
    "location",
    "content-location",
    "x-original-url",
    "forwarded",
        }
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
_SECRET_VALUE_RE = re.compile(
    r"(?i)(?:\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b|"
    r"\bgh[pousr]_[A-Za-z0-9]{20,}\b|\bAKIA[A-Z0-9]{16}\b|"
    r"\bAIza[A-Za-z0-9_-]{20,}\b|\bedt_[0-9a-f]{20,}\b|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
_PAYMENT_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")


@dataclass(frozen=True)
class SanitizedJSON:
    value: Any | None
    state: str
    redacted_fields: int = 0


class PayloadCipher:
    def __init__(self, settings: RequestLogSettings):
        settings.validate_crypto()
        assert settings.encryption_key and settings.encryption_key_id and settings.hmac_key
        try:
            self._active = Fernet(settings.encryption_key.encode("ascii"))
            self._keys = {
                key_id: Fernet(value.encode("ascii"))
                for key_id, value in settings.decryption_keys.items()
            }
        except (ValueError, TypeError) as exc:
            raise RuntimeError(
                "LOG_PAYLOAD_ENCRYPTION_KEY values must be valid Fernet keys"
            ) from exc
        try:
            decoded_hmac = base64.b64decode(
                settings.hmac_key.encode("ascii"), altchars=b"-_", validate=True
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("LOG_HMAC_KEY must be URL-safe base64") from exc
        if len(decoded_hmac) != 32:
            raise RuntimeError("LOG_HMAC_KEY must decode to exactly 32 bytes")
        self._hmac_key = decoded_hmac
        self.active_key_id = settings.encryption_key_id

    def encrypt(self, value: Any) -> tuple[str, str]:
        plaintext = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str
        ).encode("utf-8")
        ciphertext = self._active.encrypt(plaintext).decode("ascii")
        return ciphertext, hashlib.sha256(plaintext).hexdigest()

    def decrypt(self, ciphertext: str, key_id: str) -> Any:
        key = self._keys.get(key_id)
        if key is None:
            raise KeyError(f"No decryption key configured for key ID {key_id!r}")
        try:
            plaintext = key.decrypt(ciphertext.encode("ascii"))
        except InvalidToken as exc:
            raise ValueError("Stored payload failed authenticated decryption") from exc
        return json.loads(plaintext)

    def keyed_hash(self, value: str | None) -> str | None:
        if not value:
            return None
        return hmac.new(self._hmac_key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _sanitize_value(value: Any, *, depth: int = 0) -> tuple[Any, int]:
    if depth > 20:
        return "[MAX_DEPTH]", 0
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        redacted = 0
        for index, (raw_key, child) in enumerate(value.items()):
            if index >= 500:
                output["[TRUNCATED_FIELDS]"] = True
                break
            key = str(raw_key)[:256]
            if _SENSITIVE_KEY_RE.search(key):
                output[key] = REDACTED
                redacted += 1
            else:
                output[key], child_count = _sanitize_value(child, depth=depth + 1)
                redacted += child_count
        return output, redacted
    if isinstance(value, list):
        output = []
        redacted = 0
        for child in value[:500]:
            clean, child_count = _sanitize_value(child, depth=depth + 1)
            output.append(clean)
            redacted += child_count
        if len(value) > 500:
            output.append("[TRUNCATED_ITEMS]")
        return output, redacted
    if isinstance(value, str):
        clean = _JWT_RE.sub(REDACTED, value)
        clean = _BEARER_RE.sub(REDACTED, clean)
        clean = _SECRET_VALUE_RE.sub(REDACTED, clean)
        clean = _PAYMENT_CARD_RE.sub(REDACTED, clean)
        return clean[:65_536], int(clean != value)
    if value is None or isinstance(value, (bool, int, float)):
        return value, 0
    return str(value)[:4096], 0


def sanitize_json_bytes(data: bytes, *, total_size: int, limit: int) -> SanitizedJSON:
    if total_size == 0:
        return SanitizedJSON(None, "empty")
    if total_size > limit:
        return SanitizedJSON(None, "too_large")
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return SanitizedJSON(None, "invalid_json")
    value, redacted = _sanitize_value(parsed)
    return SanitizedJSON(value, "captured", redacted)


def sanitize_headers(raw_headers: Iterable[tuple[bytes, bytes]]) -> dict[str, str]:
    output: dict[str, str] = {}
    for index, (raw_name, raw_value) in enumerate(raw_headers):
        if index >= 100:
            output["x-log-truncated-headers"] = "true"
            break
        name = raw_name.decode("latin-1", "replace").lower()[:128]
        if name in _SENSITIVE_HEADERS or _SENSITIVE_KEY_RE.search(name):
            value = REDACTED
        else:
            value = raw_value.decode("latin-1", "replace")[:4096]
            value = _JWT_RE.sub(REDACTED, value)
            value = _BEARER_RE.sub(REDACTED, value)
            value = _SECRET_VALUE_RE.sub(REDACTED, value)
            value = _PAYMENT_CARD_RE.sub(REDACTED, value)
        if name in output:
            output[name] = f"{output[name]}, {value}"[:4096]
        else:
            output[name] = value
    return output


def sanitize_query_string(raw_query: bytes) -> tuple[str, int]:
    pairs = parse_qsl(raw_query.decode("utf-8", "replace"), keep_blank_values=True)
    output: list[tuple[str, str]] = []
    redacted = 0
    for key, value in pairs[:200]:
        if _SENSITIVE_KEY_RE.search(key):
            output.append((key[:256], REDACTED))
            redacted += 1
        else:
            clean = _JWT_RE.sub(REDACTED, _BEARER_RE.sub(REDACTED, value))
            clean = _SECRET_VALUE_RE.sub(REDACTED, clean)
            clean = _PAYMENT_CARD_RE.sub(REDACTED, clean)
            output.append((key[:256], clean[:4096]))
            redacted += int(clean != value)
    return urlencode(output, doseq=True), redacted
