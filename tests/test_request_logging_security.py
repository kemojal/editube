from __future__ import annotations

import base64
import asyncio
import json
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, WebSocket
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base
from app.db.log_models import ApiPayloadLog, ApiRequestLog
from app.request_logging.config import RequestLogSettings
from app.request_logging.crypto import PayloadCipher, REDACTED, sanitize_headers, sanitize_json_bytes, sanitize_query_string
from app.request_logging import database as request_log_database
from app.request_logging.middleware import RequestLoggingMiddleware
from app.request_logging.policy import classify_capture
from app.request_logging.writer import RequestLogRecord, RequestLogWriter
from app.services.request_context import begin_request, bind_route, bind_user, bind_workspace, current_request_context, end_request


def _configure(monkeypatch) -> RequestLogSettings:  # noqa: ANN001
    monkeypatch.setenv("LOG_REQUESTS_ENABLED", "1")
    monkeypatch.setenv("LOG_WRITE_DATABASE_URL", "postgresql://writer:test@db.test/editube")
    monkeypatch.setenv("LOG_READ_DATABASE_URL", "postgresql://reader:test@db.test/editube")
    monkeypatch.setenv("LOG_PAYLOAD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("LOG_PAYLOAD_ENCRYPTION_KEY_ID", "test-v1")
    monkeypatch.setenv(
        "LOG_HMAC_KEY", base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    )
    return RequestLogSettings.from_env()


def test_reader_engine_registry_lock_is_reentrant():
    """Reader factory nests engine creation under the shared registry lock."""
    lock = request_log_database._lock
    assert lock.acquire(blocking=False)
    try:
        assert lock.acquire(blocking=False)
        lock.release()
    finally:
        lock.release()


def test_crypto_round_trip_and_keyed_hash(monkeypatch):  # noqa: ANN001
    settings = _configure(monkeypatch)
    cipher = PayloadCipher(settings)
    ciphertext, digest = cipher.encrypt({"message": "safe"})
    assert "safe" not in ciphertext
    assert cipher.decrypt(ciphertext, "test-v1") == {"message": "safe"}
    assert len(digest) == 64
    assert cipher.keyed_hash("203.0.113.5") == cipher.keyed_hash("203.0.113.5")
    assert cipher.keyed_hash("203.0.113.5") != cipher.keyed_hash("203.0.113.6")


def test_ciphertext_tampering_and_unknown_keys_fail_closed(monkeypatch):  # noqa: ANN001
    settings = _configure(monkeypatch)
    cipher = PayloadCipher(settings)
    ciphertext, _ = cipher.encrypt({"message": "safe"})
    tamper_at = len(ciphertext) // 2
    replacement = "A" if ciphertext[tamper_at] != "A" else "B"
    tampered = f"{ciphertext[:tamper_at]}{replacement}{ciphertext[tamper_at + 1:]}"
    with pytest.raises(ValueError, match="authenticated decryption"):
        cipher.decrypt(tampered, "test-v1")
    with pytest.raises(KeyError, match="No decryption key"):
        cipher.decrypt(ciphertext, "retired-key-not-configured")


def test_recursive_redaction_headers_and_query():
    raw = json.dumps(
        {
            "email": "person@example.test",
            "password": "do-not-store",
            "nested": {"refresh_token": "secret", "safe": "yes"},
            "jwt_in_text": "Bearer eyJabcdefgh.abcdefghijk.abcdefghijk",
            "opaque_value": "sk_live_1234567890abcdefghij",
            "payment_reference": "4111 1111 1111 1111",
        }
    ).encode()
    result = sanitize_json_bytes(raw, total_size=len(raw), limit=65_536)
    assert result.state == "captured"
    assert result.value["password"] == REDACTED
    assert result.value["nested"]["refresh_token"] == REDACTED
    assert result.value["nested"]["safe"] == "yes"
    assert "eyJ" not in result.value["jwt_in_text"]
    assert result.value["opaque_value"] == REDACTED
    assert result.value["payment_reference"] == REDACTED
    assert result.redacted_fields >= 5

    headers = sanitize_headers(
        [(b"authorization", b"Bearer secret"), (b"cookie", b"sid=secret"), (b"origin", b"https://app.test")]
    )
    assert headers["authorization"] == REDACTED
    assert headers["cookie"] == REDACTED
    assert headers["origin"] == "https://app.test"

    query, count = sanitize_query_string(b"page=2&access_token=secret&name=demo")
    assert "secret" not in query
    assert "%5BREDACTED%5D" in query
    assert count == 1


def test_payload_size_and_invalid_json_are_never_partially_stored():
    too_large = sanitize_json_bytes(b'{"password":"abc', total_size=100_000, limit=10)
    invalid = sanitize_json_bytes(b'{"password":"abc', total_size=16, limit=100)
    assert too_large.state == "too_large" and too_large.value is None
    assert invalid.state == "invalid_json" and invalid.value is None


def test_sensitive_and_unknown_routes_are_metadata_only():
    for path, template in (
        ("/users/login", "/users/login"),
        ("/users/me/api-tokens", "/users/me/api-tokens"),
        ("/users/zoom/callback", "/users/zoom/callback"),
        ("/review/a-secret", "/review/{token}"),
        ("/internal/request-logs/x/payload", "/internal/request-logs/{log_id}/payload"),
    ):
        decision = classify_capture(
            method="POST",
            path=path,
            route_template=template,
            request_content_type="application/json",
            response_content_type="application/json",
            response_streaming=False,
        )
        assert not decision.capture_payload, path
    unknown = classify_capture(
        method="POST",
        path="/future-route",
        route_template=None,
        request_content_type="application/json",
        response_content_type="application/json",
        response_streaming=False,
    )
    assert not unknown.capture_payload and unknown.reason == "unknown_route"


def test_all_registered_token_oauth_and_webhook_routes_are_metadata_only(monkeypatch):  # noqa: ANN001
    monkeypatch.setenv("LOG_REQUESTS_ENABLED", "0")
    from app.main import app as public_app

    failures = []
    for route in public_app.routes:
        path = getattr(route, "path", None)
        if not path or not any(
            marker in path.lower()
            for marker in ("token", "mfa", "oauth", "callback", "webhook")
        ):
            continue
        methods = getattr(route, "methods", None) or {"GET"}
        decision = classify_capture(
            method=next(iter(methods)),
            path=path,
            route_template=path,
            request_content_type="application/json",
            response_content_type="application/json",
            response_streaming=False,
        )
        if decision.capture_payload:
            failures.append(path)
    assert failures == []


def test_asgi_middleware_captures_redacted_json_without_changing_response(monkeypatch):  # noqa: ANN001
    settings = _configure(monkeypatch)
    records = []
    fake_writer = SimpleNamespace(enqueue=lambda record: records.append(record) or True)
    monkeypatch.setattr(
        "app.request_logging.middleware.get_global_writer", lambda _settings: fake_writer
    )

    app = FastAPI()

    @app.post("/echo/{item_id}")
    async def echo(item_id: int, body: dict):
        return {"item_id": item_id, "received": body, "access_token": "response-secret"}

    app.add_middleware(RequestLoggingMiddleware)
    with TestClient(app) as client:
        response = client.post(
            "/echo/42?mode=debug&token=query-secret",
            headers={"Authorization": "Bearer request-secret", "X-Custom": "kept"},
            json={"title": "broken clip", "password": "body-secret"},
        )
    assert response.status_code == 200
    assert response.json()["received"]["password"] == "body-secret"
    assert len(records) == 1
    record = records[0]
    assert record.request["route_template"] == "/echo/{item_id}"
    assert record.request["request_headers"]["authorization"] == REDACTED
    assert record.request["request_headers"]["x-custom"] == "kept"
    assert record.request["payload_present"] is True

    cipher = PayloadCipher(settings)
    request_payload = cipher.decrypt(record.payload["request_ciphertext"], "test-v1")
    response_payload = cipher.decrypt(record.payload["response_ciphertext"], "test-v1")
    assert request_payload["path"] == "/echo/42"
    assert "query-secret" not in request_payload["query"]
    assert request_payload["body"]["password"] == REDACTED
    assert response_payload["body"]["access_token"] == REDACTED


def test_asgi_middleware_records_sensitive_route_without_payload(monkeypatch):  # noqa: ANN001
    _configure(monkeypatch)
    records = []
    fake_writer = SimpleNamespace(enqueue=lambda record: records.append(record) or True)
    monkeypatch.setattr(
        "app.request_logging.middleware.get_global_writer", lambda _settings: fake_writer
    )
    app = FastAPI()

    @app.post("/users/login")
    async def login(body: dict):
        return {"access_token": "must-never-be-captured", "echo": body}

    app.add_middleware(RequestLoggingMiddleware)
    with TestClient(app) as client:
        assert client.post("/users/login", json={"password": "secret"}).status_code == 200
    assert records[0].request["capture_reason"] == "sensitive_or_operational_route"
    assert records[0].request["payload_present"] is False
    assert records[0].payload is None


def test_asgi_middleware_records_unhandled_failure_class(monkeypatch):  # noqa: ANN001
    _configure(monkeypatch)
    records = []
    fake_writer = SimpleNamespace(enqueue=lambda record: records.append(record) or True)
    monkeypatch.setattr(
        "app.request_logging.middleware.get_global_writer", lambda _settings: fake_writer
    )
    app = FastAPI()

    @app.get("/explode")
    async def explode():
        raise RuntimeError("sensitive diagnostic must not be metadata")

    app.add_middleware(RequestLoggingMiddleware)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/explode")
    assert response.status_code == 500
    assert records[0].request["status_code"] == 500
    assert records[0].request["error_class"] == "RuntimeError"
    assert "sensitive diagnostic" not in json.dumps(records[0].request, default=str)


def test_asgi_middleware_excludes_websockets_entirely(monkeypatch):  # noqa: ANN001
    _configure(monkeypatch)
    records = []
    monkeypatch.setattr(
        "app.request_logging.middleware.get_global_writer",
        lambda _settings: SimpleNamespace(enqueue=lambda record: records.append(record)),
    )
    app = FastAPI()

    @app.websocket("/events")
    async def events(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("ready")
        await websocket.close()

    app.add_middleware(RequestLoggingMiddleware)
    with TestClient(app) as client:
        with client.websocket_connect("/events") as websocket:
            assert websocket.receive_text() == "ready"
    assert records == []


def test_streaming_json_response_is_metadata_only(monkeypatch):  # noqa: ANN001
    _configure(monkeypatch)
    records = []
    fake_writer = SimpleNamespace(enqueue=lambda record: records.append(record) or True)
    monkeypatch.setattr(
        "app.request_logging.middleware.get_global_writer", lambda _settings: fake_writer
    )
    app = FastAPI()

    @app.get("/stream")
    async def stream():
        async def chunks():
            yield b'{"items":['
            yield b"]}"

        return StreamingResponse(chunks(), media_type="application/json")

    app.add_middleware(RequestLoggingMiddleware)
    with TestClient(app) as client:
        response = client.get("/stream")
    assert response.status_code == 200
    assert response.json() == {"items": []}
    assert records[0].request["capture_reason"] == "streaming_response"
    assert records[0].request["payload_present"] is False
    assert records[0].payload is None


def test_writer_persists_request_and_ciphertext_in_one_transaction(monkeypatch):  # noqa: ANN001
    settings = _configure(monkeypatch)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _attach_log_schema(connection, _record):  # noqa: ANN001
        connection.execute("ATTACH DATABASE ':memory:' AS log")

    Base.metadata.create_all(
        engine, tables=[ApiRequestLog.__table__, ApiPayloadLog.__table__]
    )
    monkeypatch.setattr("app.request_logging.writer.write_engine", lambda _settings: engine)
    cipher = PayloadCipher(settings)
    ciphertext, digest = cipher.encrypt({"body": {"safe": True}})
    log_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    record = RequestLogRecord(
        request={
            "id": log_id,
            "request_id": "request-123",
            "occurred_at": now,
            "completed_at": now,
            "environment": "test",
            "release": "sha",
            "method": "POST",
            "route_template": "/items/{item_id}",
            "endpoint_name": "create_item",
            "status_code": 500,
            "duration_ms": 12,
            "request_size_bytes": 10,
            "response_size_bytes": 20,
            "request_content_type": "application/json",
            "response_content_type": "application/json",
            "request_headers": {"authorization": REDACTED},
            "response_headers": {},
            "client_ip_hash": "a" * 64,
            "user_agent_hash": "b" * 64,
            "user_id": 7,
            "workspace_id": 9,
            "trace_id": None,
            "capture_reason": "eligible_json",
            "request_body_state": "captured",
            "response_body_state": "captured",
            "error_class": "RuntimeError",
            "payload_present": True,
        },
        payload={
            "request_log_id": log_id,
            "key_id": "test-v1",
            "algorithm": "fernet",
            "request_ciphertext": ciphertext,
            "request_sha256": digest,
            "response_ciphertext": None,
            "response_sha256": None,
            "redaction_summary": {"request_fields": 1},
            "expires_at": now + timedelta(days=14),
        },
    )
    writer = RequestLogWriter(settings)
    writer._write_batch([record])
    db = sessionmaker(bind=engine)()
    try:
        stored_request = db.query(ApiRequestLog).one()
        stored_payload = db.query(ApiPayloadLog).one()
        assert stored_request.request_id == "request-123"
        assert stored_request.payload_present is True
        assert "safe" not in stored_payload.request_ciphertext
        assert cipher.decrypt(stored_payload.request_ciphertext, "test-v1")["body"]["safe"] is True
    finally:
        db.close()
        engine.dispose()


def test_writer_queue_is_bounded_and_drops_without_blocking(monkeypatch):  # noqa: ANN001
    settings = replace(_configure(monkeypatch), queue_max_size=1)
    writer = RequestLogWriter(settings)
    monkeypatch.setattr(writer, "start", lambda: None)
    record = RequestLogRecord(request={})
    assert writer.enqueue(record) is True
    assert writer.enqueue(record) is False
    assert writer.dropped == 1


def test_writer_sheds_payload_before_metadata_under_queue_pressure(monkeypatch):  # noqa: ANN001
    settings = replace(_configure(monkeypatch), queue_max_size=10)
    writer = RequestLogWriter(settings)
    monkeypatch.setattr(writer, "start", lambda: None)
    metadata = {"payload_present": True, "capture_reason": "eligible_json"}
    for _ in range(8):
        assert writer.enqueue(RequestLogRecord(request=metadata, payload=None)) is True
    assert writer.enqueue(RequestLogRecord(request=metadata, payload={"ciphertext": "x"})) is True
    last = writer.queue.get_nowait()
    while not writer.queue.empty():
        last = writer.queue.get_nowait()
    assert last.payload is None
    assert last.request["capture_reason"] == "queue_pressure_metadata_only"
    assert writer.payloads_shed == 1


def test_request_context_mutations_propagate_but_do_not_leak_after_reset():
    assert current_request_context().route_template is None
    token = begin_request(request_id="request-context-1", trace_id=None, analytics_session_id=None)
    bind_route("/inside/{id}")
    assert current_request_context().route_template == "/inside/{id}"
    end_request(token)
    assert current_request_context().route_template is None


def test_sync_dependency_context_bindings_flow_back_to_asgi_task():
    token = begin_request(request_id="request-context-thread", trace_id=None, analytics_session_id=None)

    def _sync_dependency_work() -> None:
        bind_user(
            user_id=77,
            plan="pro",
            subscription_status="active",
            user_role="creator",
        )
        bind_workspace(88)

    asyncio.run(asyncio.to_thread(_sync_dependency_work))
    assert current_request_context().user_id == 77
    assert current_request_context().workspace_id == 88
    end_request(token)


def test_privileged_routes_are_only_mounted_on_internal_admin_app(monkeypatch):  # noqa: ANN001
    monkeypatch.setenv("LOG_REQUESTS_ENABLED", "0")
    from app.internal_admin import app as internal_app
    from app.main import app as public_app

    # FastAPI 0.141 stores included routers lazily, so ``app.routes`` contains
    # an internal router wrapper instead of a flattened list of path objects.
    # The generated schema resolves that wrapper and reflects the paths that
    # are actually exposed by each application.
    public_paths = set(public_app.openapi().get("paths", {}))
    internal_paths = set(internal_app.openapi().get("paths", {}))
    assert not any(path.startswith("/internal/request-logs") for path in public_paths)
    assert "/internal/request-logs" in internal_paths
    assert internal_app.docs_url is None
    assert internal_app.openapi_url is None


def test_public_and_internal_processes_reject_each_others_database_credentials(
    monkeypatch,
):  # noqa: ANN001
    _configure(monkeypatch)
    from app.internal_admin import _lifespan as internal_lifespan
    from app.internal_admin import app as internal_app
    from app.main import _app_lifespan as public_lifespan
    from app.main import app as public_app

    async def enter_public() -> None:
        async with public_lifespan(public_app):
            pass

    with pytest.raises(RuntimeError, match="Public API must not receive"):
        asyncio.run(enter_public())

    async def enter_internal() -> None:
        async with internal_lifespan(internal_app):
            pass

    with pytest.raises(RuntimeError, match="Internal admin service must not receive"):
        asyncio.run(enter_internal())
