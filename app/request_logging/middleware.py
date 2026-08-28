from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from app.services.request_context import current_request_context

from .config import RequestLogSettings
from .crypto import PayloadCipher, sanitize_headers, sanitize_json_bytes, sanitize_query_string
from .policy import classify_capture, content_type_is_json, route_details
from .writer import RequestLogRecord, get_global_writer


logger = logging.getLogger(__name__)
ASGIApp = Callable[[dict, Callable[[], Awaitable[dict]], Callable[[dict], Awaitable[None]]], Awaitable[None]]


def _header(headers: list[tuple[bytes, bytes]], name: str) -> str | None:
    wanted = name.lower().encode("ascii")
    for key, value in headers:
        if key.lower() == wanted:
            return value.decode("latin-1", "replace")[:4096]
    return None


def _append_bounded(target: bytearray, chunk: bytes, limit: int) -> None:
    remaining = limit - len(target)
    if remaining > 0:
        target.extend(chunk[:remaining])


class RequestLoggingMiddleware:
    """Pure ASGI capture middleware.

    It observes receive/send messages without consuming or replacing them, so
    uploads, streaming responses, disconnects, and exception handling retain
    their original behaviour. Persistence happens on a bounded background
    queue and can never fail the customer request.
    """

    def __init__(self, app: ASGIApp):
        self.app = app
        self.settings = RequestLogSettings.from_env()
        self.cipher = PayloadCipher(self.settings) if self.settings.enabled else None

    async def __call__(self, scope: dict, receive, send) -> None:  # noqa: ANN001
        if scope.get("type") != "http" or not self.settings.enabled:
            await self.app(scope, receive, send)
            return

        started_perf = time.perf_counter()
        occurred_at = datetime.now(timezone.utc)
        request_headers_raw = list(scope.get("headers") or [])
        request_content_type = _header(request_headers_raw, "content-type")
        request_buffer = bytearray()
        response_buffer = bytearray()
        request_size = 0
        response_size = 0
        response_status = 500
        response_headers_raw: list[tuple[bytes, bytes]] = []
        response_content_type: str | None = None
        response_streaming = False
        caught_error: BaseException | None = None

        async def logging_receive() -> dict:
            nonlocal request_size
            message = await receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                request_size += len(body)
                _append_bounded(request_buffer, body, self.settings.request_body_limit)
            return message

        async def logging_send(message: dict) -> None:
            nonlocal response_status, response_headers_raw, response_content_type
            nonlocal response_streaming, response_size
            if message.get("type") == "http.response.start":
                response_status = int(message.get("status", 500))
                response_headers_raw = list(message.get("headers") or [])
                response_content_type = _header(response_headers_raw, "content-type")
            elif message.get("type") == "http.response.body":
                body = message.get("body", b"")
                response_size += len(body)
                _append_bounded(response_buffer, body, self.settings.response_body_limit)
                response_streaming = response_streaming or bool(message.get("more_body"))
            await send(message)

        try:
            await self.app(scope, logging_receive, logging_send)
        except BaseException as exc:
            caught_error = exc
            raise
        finally:
            try:
                completed_at = datetime.now(timezone.utc)
                route_template, endpoint_name = route_details(scope)
                path = str(scope.get("path") or "")[:4096]
                method = str(scope.get("method") or "UNKNOWN")[:16].upper()
                decision = classify_capture(
                    method=method,
                    path=path,
                    route_template=route_template,
                    request_content_type=request_content_type,
                    response_content_type=response_content_type,
                    response_streaming=response_streaming,
                )
                request_body_state = "excluded"
                response_body_state = "excluded"
                request_value: Any | None = None
                response_value: Any | None = None
                request_redactions = 0
                response_redactions = 0

                if decision.capture_payload:
                    if content_type_is_json(request_content_type):
                        sanitized_request = sanitize_json_bytes(
                            bytes(request_buffer),
                            total_size=request_size,
                            limit=self.settings.request_body_limit,
                        )
                        request_value = sanitized_request.value
                        request_body_state = sanitized_request.state
                        request_redactions = sanitized_request.redacted_fields
                    else:
                        request_body_state = "non_json"
                    if content_type_is_json(response_content_type):
                        sanitized_response = sanitize_json_bytes(
                            bytes(response_buffer),
                            total_size=response_size,
                            limit=self.settings.response_body_limit,
                        )
                        response_value = sanitized_response.value
                        response_body_state = sanitized_response.state
                        response_redactions = sanitized_response.redacted_fields
                    else:
                        response_body_state = "non_json"

                query, query_redactions = sanitize_query_string(scope.get("query_string", b""))
                request_ciphertext = request_sha256 = None
                response_ciphertext = response_sha256 = None
                if decision.capture_payload and self.cipher:
                    request_envelope = {
                        "path": path,
                        "query": query,
                        "body": request_value,
                    }
                    request_ciphertext, request_sha256 = self.cipher.encrypt(request_envelope)
                    if response_body_state == "captured":
                        response_ciphertext, response_sha256 = self.cipher.encrypt(
                            {"body": response_value}
                        )

                payload_present = bool(request_ciphertext or response_ciphertext)
                log_id = uuid.uuid4()
                context = current_request_context()
                state = scope.get("state") or {}
                request_id = (
                    getattr(context, "request_id", None)
                    or state.get("request_id")
                    or str(uuid.uuid4())
                )
                client = scope.get("client") or (None, None)
                client_ip = str(client[0]) if client and client[0] else None
                user_agent = _header(request_headers_raw, "user-agent")
                status_code = 500 if caught_error is not None and response_status < 500 else response_status
                duration_ms = max(0, int((time.perf_counter() - started_perf) * 1000))
                error_class = None
                if caught_error is not None:
                    error_class = type(caught_error).__name__[:128]
                elif status_code >= 400:
                    error_class = f"http_{status_code}"

                record = RequestLogRecord(
                    request={
                        "id": log_id,
                        "request_id": str(request_id)[:128],
                        "occurred_at": occurred_at,
                        "completed_at": completed_at,
                        "environment": self.settings.environment[:32],
                        "release": self.settings.release,
                        "method": method,
                        "route_template": route_template,
                        "endpoint_name": endpoint_name,
                        "status_code": status_code,
                        "duration_ms": duration_ms,
                        "request_size_bytes": request_size,
                        "response_size_bytes": response_size,
                        "request_content_type": request_content_type,
                        "response_content_type": response_content_type,
                        "request_headers": sanitize_headers(request_headers_raw),
                        "response_headers": sanitize_headers(response_headers_raw),
                        "client_ip_hash": self.cipher.keyed_hash(client_ip) if self.cipher else None,
                        "user_agent_hash": self.cipher.keyed_hash(user_agent) if self.cipher else None,
                        "user_id": getattr(context, "user_id", None),
                        "workspace_id": getattr(context, "workspace_id", None),
                        "trace_id": getattr(context, "trace_id", None),
                        "capture_reason": decision.reason,
                        "request_body_state": request_body_state,
                        "response_body_state": response_body_state,
                        "error_class": error_class,
                        "payload_present": payload_present,
                    },
                    payload=(
                        {
                            "request_log_id": log_id,
                            "key_id": self.cipher.active_key_id,
                            "algorithm": "fernet",
                            "request_ciphertext": request_ciphertext,
                            "request_sha256": request_sha256,
                            "response_ciphertext": response_ciphertext,
                            "response_sha256": response_sha256,
                            "redaction_summary": {
                                "request_fields": request_redactions,
                                "response_fields": response_redactions,
                                "query_fields": query_redactions,
                            },
                            "expires_at": completed_at
                            + timedelta(
                                days=(
                                    self.settings.failure_payload_retention_days
                                    if status_code >= 400
                                    else self.settings.success_payload_retention_days
                                )
                            ),
                        }
                        if payload_present and self.cipher
                        else None
                    ),
                )
                get_global_writer(self.settings).enqueue(record)
            except Exception:  # noqa: BLE001
                logger.exception("Request-log capture failed open")
