"""Privacy-safe Sentry initialization shared by API and worker processes."""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit, urlunsplit


_SENSITIVE_PARTS = (
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
    "transcript",
    "prompt",
    "comment",
    "message",
    "content",
    "body",
    "signed_url",
)

_EMAIL_RE = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_BEARER_RE = re.compile(r"\bbearer\s+[a-z0-9._~+/=-]+", re.IGNORECASE)
_SECRET_RE = re.compile(r"\b(?:sk|rk)_(?:live|test)_[a-z0-9]+\b", re.IGNORECASE)
_URL_RE = re.compile(r"(?:https?://|data:)[^\s\"'<>]+", re.IGNORECASE)


def _scrub_string(value: str) -> str:
    value = _EMAIL_RE.sub("[Filtered Email]", value)
    value = _BEARER_RE.sub("[Filtered Credential]", value)
    value = _SECRET_RE.sub("[Filtered Credential]", value)
    value = _URL_RE.sub("[Filtered URL]", value)
    return value[:1000]


def _scrub_frame_location(value: Any) -> Any:
    """Preserve symbolication paths while removing signed query/fragment data."""

    if not isinstance(value, str):
        return value
    if value.lower().startswith("data:"):
        return "[Filtered URL]"
    if value.lower().startswith(("http://", "https://")):
        try:
            parsed = urlsplit(value)
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))[:1000]
        except ValueError:
            return "[Filtered URL]"
    return _scrub_string(value)


def _scrub(value: Any, depth: int = 0) -> Any:
    if depth > 5:
        return "[Filtered]"
    if isinstance(value, dict):
        return {
            str(key): (
                "[Filtered]"
                if any(part in str(key).lower() for part in _SENSITIVE_PARTS)
                else _scrub(item, depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_scrub(item, depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return _scrub_string(value)
    return value


def _scrub_exception(value: Any) -> Any:
    clean = _scrub(value)
    if not isinstance(value, dict) or not isinstance(clean, dict):
        return clean
    raw_values = value.get("values")
    clean_values = clean.get("values")
    if not isinstance(raw_values, list) or not isinstance(clean_values, list):
        return clean
    for raw_exception, clean_exception in zip(raw_values[:20], clean_values[:20]):
        if not isinstance(raw_exception, dict) or not isinstance(clean_exception, dict):
            continue
        raw_stack = raw_exception.get("stacktrace")
        clean_stack = clean_exception.get("stacktrace")
        if not isinstance(raw_stack, dict) or not isinstance(clean_stack, dict):
            continue
        raw_frames = raw_stack.get("frames")
        clean_frames = clean_stack.get("frames")
        if not isinstance(raw_frames, list) or not isinstance(clean_frames, list):
            continue
        for raw_frame, clean_frame in zip(raw_frames[:200], clean_frames[:200]):
            if not isinstance(raw_frame, dict) or not isinstance(clean_frame, dict):
                continue
            for field in ("filename", "abs_path"):
                if field in raw_frame:
                    clean_frame[field] = _scrub_frame_location(raw_frame[field])
            for field in ("function", "module", "package", "instruction_addr"):
                if isinstance(raw_frame.get(field), str):
                    clean_frame[field] = _scrub_string(raw_frame[field])
            for field in ("lineno", "colno", "in_app"):
                if isinstance(raw_frame.get(field), (bool, int)):
                    clean_frame[field] = raw_frame[field]
    return clean


def before_send(event: dict, hint: dict | None = None) -> dict:  # noqa: ARG001
    request = event.get("request")
    if isinstance(request, dict):
        request.pop("data", None)
        request.pop("cookies", None)
        request.pop("query_string", None)
        request.pop("url", None)
        request.pop("env", None)
        headers = request.get("headers")
        if isinstance(headers, dict):
            request["headers"] = {
                key: value
                for key, value in headers.items()
                if key.lower() in {"content-type", "x-request-id"}
            }
    if isinstance(event.get("user"), dict):
        event["user"] = {"id": event["user"].get("id")}
    for key in ("extra", "contexts", "tags", "breadcrumbs", "logentry"):
        if key in event:
            event[key] = _scrub(event[key])
    if "exception" in event:
        event["exception"] = _scrub_exception(event["exception"])
    if isinstance(event.get("message"), str):
        event["message"] = _scrub_string(event["message"])
    return event


def _sentry_dsn_for_role(process_role: str) -> str:
    role_name = process_role.strip().upper()
    role_dsn = os.getenv(f"SENTRY_{role_name}_DSN") if role_name else None
    return (role_dsn or os.getenv("SENTRY_DSN") or "").strip()


def init_sentry(process_role: str) -> bool:
    dsn = _sentry_dsn_for_role(process_role)
    if not dsn:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.rq import RqIntegration
    except ImportError:
        return False

    integrations = [RqIntegration()] if process_role == "worker" else [FastApiIntegration()]
    sentry_sdk.init(
        dsn=dsn,
        environment=(os.getenv("APP_ENV") or os.getenv("ENVIRONMENT") or "local"),
        release=(os.getenv("RELEASE") or os.getenv("GIT_SHA") or None),
        integrations=integrations,
        send_default_pii=False,
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
        profiles_sample_rate=float(os.getenv("SENTRY_PROFILES_SAMPLE_RATE", "0")),
        before_send=before_send,
    )
    sentry_sdk.set_tag("process_role", process_role)
    return True


def capture_exception(exc: BaseException) -> None:
    try:
        import sentry_sdk

        sentry_sdk.capture_exception(exc)
    except ImportError:
        return


@contextmanager
def observed_span(operation: str, name: str, **attributes: str | int | bool | None):
    """Create a provider-safe span and degrade to a no-op without Sentry."""

    try:
        import sentry_sdk
    except ImportError:
        yield None
        return

    with sentry_sdk.start_span(op=operation[:64], name=name[:200]) as span:
        for key, value in attributes.items():
            if value is not None:
                span.set_data(key[:80], value)
        yield span


def set_observability_tags(**tags: str | int | bool | None) -> None:
    try:
        import sentry_sdk
    except ImportError:
        return
    for key, value in tags.items():
        if value is not None:
            sentry_sdk.set_tag(key[:80], str(value)[:200])


def capture_message(message: str, *, level: str = "warning", **tags: str) -> None:
    try:
        import sentry_sdk
    except ImportError:
        return
    with sentry_sdk.push_scope() as scope:
        for key, value in tags.items():
            scope.set_tag(key[:80], value[:200])
        sentry_sdk.capture_message(message[:500], level=level)
