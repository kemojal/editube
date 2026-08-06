from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
import threading
import time
from urllib.parse import quote as _url_quote
from typing import Iterable

#: Shown as the account issuer in authenticator apps. Overridable so a staging
#: deployment does not enroll under the same name as production — two entries
#: both called "Editube" in Google Authenticator are indistinguishable.
TOTP_ISSUER = os.getenv("MFA_TOTP_ISSUER", "Editube")


def generate_totp_secret() -> str:
    raw = secrets.token_bytes(20)
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def build_otpauth_url(secret: str, account_label: str, *, issuer: str | None = None) -> str:
    """`otpauth://` URI for the enrollment QR code.

    Both the issuer prefix in the path and the `issuer` parameter are required:
    older apps read one, newer apps read the other, and an app that finds
    neither files the entry under a blank name. Every component is
    percent-encoded — an email with a `+` tag silently truncated the label.
    """
    issuer_name = issuer or TOTP_ISSUER
    label = _url_quote(f"{issuer_name}:{account_label}", safe="")
    params = (
        f"secret={secret}"
        f"&issuer={_url_quote(issuer_name, safe='')}"
        "&algorithm=SHA1&digits=6&period=30"
    )
    return f"otpauth://totp/{label}?{params}"


def _decode_base32_secret(secret: str) -> bytes:
    padded = secret.upper() + "=" * ((8 - len(secret) % 8) % 8)
    return base64.b32decode(padded.encode("ascii"), casefold=True)


def _hotp(secret: str, counter: int, digits: int = 6) -> str:
    key = _decode_base32_secret(secret)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**digits)
    return str(code_int).zfill(digits)


def verify_totp_code(secret: str, code: str, *, interval_seconds: int = 30, drift_windows: int = 1) -> bool:
    now_counter = int(time.time() // interval_seconds)
    clean = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(clean) != 6:
        return False
    for delta in range(-drift_windows, drift_windows + 1):
        if hmac.compare_digest(_hotp(secret, now_counter + delta), clean):
            return True
    return False


def generate_recovery_codes(count: int = 10) -> list[str]:
    out: list[str] = []
    for _ in range(count):
        token = secrets.token_hex(4)
        out.append(f"{token[:4]}-{token[4:]}")
    return out


def normalize_recovery_code(raw_code: str) -> str:
    """Codes are shown as `a1b2-c3d4` but get typed back every other way.

    Hashing the raw string meant a user who typed their own code in caps, or
    pasted it without the dash, was told it was invalid. Case and separators
    carry no entropy, so they are stripped before hashing on both sides.
    """
    cleaned = "".join(ch for ch in (raw_code or "").lower() if ch.isalnum())
    if len(cleaned) == 8:
        return f"{cleaned[:4]}-{cleaned[4:]}"
    return cleaned


def hash_recovery_codes(codes: Iterable[str]) -> list[str]:
    pepper = os.getenv("MFA_RECOVERY_PEPPER", "editube-mfa-pepper")
    return [
        hashlib.sha256(f"{pepper}:{normalize_recovery_code(code)}".encode("utf-8")).hexdigest()
        for code in codes
    ]


def verify_recovery_code(raw_code: str, hashed_codes: Iterable[str]) -> str | None:
    normalized = normalize_recovery_code(raw_code)
    if not normalized:
        return None
    pepper = os.getenv("MFA_RECOVERY_PEPPER", "editube-mfa-pepper")
    candidate = hashlib.sha256(f"{pepper}:{normalized}".encode("utf-8")).hexdigest()
    matched: str | None = None
    for hashed in hashed_codes:
        # No early return: comparing against every stored hash keeps the work
        # constant regardless of which code matched.
        if hmac.compare_digest(candidate, hashed):
            matched = hashed
    return matched


# ---------------------------------------------------------------------------
# Attempt throttling
# ---------------------------------------------------------------------------

#: A 6-digit code is 10^6 wide and stays valid for ~90s across the drift
#: window, so an unthrottled endpoint is brute-forceable in minutes. This caps
#: attempts per user.
#:
#: In-process by design: the app has no shared cache dependency. With multiple
#: workers the effective ceiling is `MFA_MAX_ATTEMPTS * worker_count`, which
#: still leaves brute force far out of reach. Move to a shared store if the
#: deployment ever needs an exact limit.
MFA_MAX_ATTEMPTS = int(os.getenv("MFA_MAX_ATTEMPTS", "8"))
MFA_ATTEMPT_WINDOW_SECONDS = int(os.getenv("MFA_ATTEMPT_WINDOW_SECONDS", "300"))

_attempts: dict[str, list[float]] = {}
_attempts_lock = threading.Lock()


def _prune(bucket: list[float], now: float) -> list[float]:
    return [ts for ts in bucket if now - ts < MFA_ATTEMPT_WINDOW_SECONDS]


def mfa_attempts_remaining(key: str) -> int:
    now = time.time()
    with _attempts_lock:
        bucket = _prune(_attempts.get(key, []), now)
        _attempts[key] = bucket
        return max(0, MFA_MAX_ATTEMPTS - len(bucket))


def record_failed_mfa_attempt(key: str) -> int:
    """Record a failure. Returns attempts remaining after it."""
    now = time.time()
    with _attempts_lock:
        bucket = _prune(_attempts.get(key, []), now)
        bucket.append(now)
        _attempts[key] = bucket
        return max(0, MFA_MAX_ATTEMPTS - len(bucket))


def clear_mfa_attempts(key: str) -> None:
    """Called on success so a legitimate user isn't punished for fat fingers."""
    with _attempts_lock:
        _attempts.pop(key, None)
