from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
from typing import Iterable

def generate_totp_secret() -> str:
    raw = secrets.token_bytes(20)
    return base64.b32encode(raw).decode("ascii").rstrip("=")


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


def hash_recovery_codes(codes: Iterable[str]) -> list[str]:
    pepper = os.getenv("MFA_RECOVERY_PEPPER", "editube-mfa-pepper")
    return [
        hashlib.sha256(f"{pepper}:{code}".encode("utf-8")).hexdigest()
        for code in codes
    ]


def verify_recovery_code(raw_code: str, hashed_codes: Iterable[str]) -> str | None:
    pepper = os.getenv("MFA_RECOVERY_PEPPER", "editube-mfa-pepper")
    candidate = hashlib.sha256(f"{pepper}:{raw_code}".encode("utf-8")).hexdigest()
    for hashed in hashed_codes:
        if hmac.compare_digest(candidate, hashed):
            return hashed
    return None
