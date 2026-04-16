from __future__ import annotations

import os
from functools import lru_cache
from urllib import request
import json


_ISO_FALLBACK = "ZZ"


def extract_client_ip(headers: dict[str, str], direct_ip: str | None) -> str | None:
    trusted = (os.getenv("TRUST_PROXY_HEADERS", "false").lower() in ("1", "true", "yes"))
    if trusted:
        forwarded = headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip() or direct_ip
        real_ip = headers.get("x-real-ip", "").strip()
        if real_ip:
            return real_ip
    return direct_ip


@lru_cache(maxsize=4096)
def resolve_country_code(ip_address: str | None) -> str:
    if not ip_address:
        return _ISO_FALLBACK
    # Lightweight default resolver. A provider-backed lookup can replace this.
    # RFC1918/local ranges are treated as unknown.
    if ip_address.startswith("10.") or ip_address.startswith("192.168.") or ip_address.startswith("127."):
        return _ISO_FALLBACK
    if ip_address.startswith("172."):
        return _ISO_FALLBACK
    # Environment override for deterministic tests.
    forced = os.getenv("GEOIP_FORCE_COUNTRY", "").strip().upper()
    if len(forced) == 2:
        return forced
    provider = os.getenv("GEOIP_PROVIDER", "").strip().lower()
    if provider == "ipapi":
        try:
            url = f"https://ipapi.co/{ip_address}/json/"
            req = request.Request(url, method="GET")
            with request.urlopen(req, timeout=2.5) as response:
                payload = json.loads(response.read().decode("utf-8"))
                cc = (payload.get("country_code") or "").strip().upper()
                if len(cc) == 2:
                    return cc
        except Exception:
            return _ISO_FALLBACK
    return _ISO_FALLBACK


def is_country_allowed(
    *,
    mode: str,
    allow_countries: list[str] | None,
    block_countries: list[str] | None,
    country_code: str,
) -> bool:
    cc = (country_code or _ISO_FALLBACK).upper()
    allow = {(x or "").upper() for x in (allow_countries or []) if x}
    block = {(x or "").upper() for x in (block_countries or []) if x}
    norm_mode = (mode or "off").lower()
    if norm_mode == "allowlist":
        return cc in allow
    if norm_mode == "blocklist":
        return cc not in block
    return True
