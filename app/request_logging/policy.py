from __future__ import annotations

import re
from dataclasses import dataclass


_JSON_CONTENT_TYPES = {"application/json", "application/problem+json"}
_METADATA_ONLY_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^/$",
        r"^/(health|docs|redoc|openapi\.json)(/|$)",
        r"^/uploads(/|$)",
        r"^/internal/request-logs(/|$)",
        r"^/token(/|$)",
        r"^/users/(login|register|refresh|forgot|reset|password)(/|$)",
        r"^/users/mfa(/|$)",
        r"^/users/me/mfa(/|$)",
        r"^/users/(me/)?api-tokens(/|$)",
        r"^/users/google(/|$)",
        r"^/users/(zoom|sso)(/|$)",
        r"^/billing/.*webhook",
        r"^/public/referrals/email-events",
        r"token",
        r"oauth|callback",
        r"webhook",
    )
)


@dataclass(frozen=True)
class CaptureDecision:
    capture_payload: bool
    reason: str


def content_type_is_json(content_type: str | None) -> bool:
    if not content_type:
        return False
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type in _JSON_CONTENT_TYPES or media_type.endswith("+json")


def classify_capture(
    *,
    method: str,
    path: str,
    route_template: str | None,
    request_content_type: str | None,
    response_content_type: str | None,
    response_streaming: bool,
) -> CaptureDecision:
    if method.upper() == "OPTIONS":
        return CaptureDecision(False, "options")
    if route_template is None:
        return CaptureDecision(False, "unknown_route")
    for pattern in _METADATA_ONLY_PATTERNS:
        if pattern.search(path) or pattern.search(route_template):
            return CaptureDecision(False, "sensitive_or_operational_route")
    if "{token}" in route_template.lower() or "{code}" in route_template.lower():
        return CaptureDecision(False, "secret_in_route")
    if response_streaming:
        return CaptureDecision(False, "streaming_response")
    request_json = content_type_is_json(request_content_type)
    response_json = content_type_is_json(response_content_type)
    if not request_json and not response_json:
        return CaptureDecision(False, "non_json")
    return CaptureDecision(True, "eligible_json")


def route_details(scope: dict) -> tuple[str | None, str | None]:
    route = scope.get("route")
    template = getattr(route, "path", None)
    endpoint = scope.get("endpoint") or getattr(route, "endpoint", None)
    endpoint_name = getattr(endpoint, "__name__", None)
    return template, endpoint_name
