import asyncio
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import api_router
from .db.database import SessionLocal, get_db
from .utils.security import authenticate_access_token
from .websocket_manager import notifications_ws_manager, review_room_ws_manager
from sqlalchemy.orm import Session, joinedload

from .db.models import (
    Clip,
    ReviewLink,
    ReviewSession,
    ReviewRoomMessage,
    TeamVideoRoomMessage,
    Video,
    Project,
)
from .services.project_access import can_access_project, can_moderate_video_comments
from .services.observability import capture_exception, init_sentry
from .services.product_analytics import emit_after_commit, emit_once
from .services.request_context import (
    begin_request,
    bind_route,
    current_request_context,
    end_request,
)

load_dotenv()

init_sentry("api")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _record_dependency_degraded(dependency_key: str, status: str, error_code: str) -> None:
    hour = time.strftime("%Y%m%d%H", time.gmtime())
    emit_after_commit(
        "dependency_degraded",
        event_id=f"dependency:{dependency_key}:{status}:{hour}",
        properties={
            "dependency_key": dependency_key,
            "dependency_status": status,
            "error_code": error_code,
            "result": "failure",
        },
    )


@asynccontextmanager
async def _app_lifespan(app: FastAPI):
    """
    Optional in-process scheduler for mention digest batch enqueue.
    Prefer external cron calling the same RQ job in production; see docs/review_nle_integration.md.
    """
    digest_hours_raw = os.getenv("MENTION_DIGEST_INTERVAL_HOURS", "").strip()
    digest_hours = float(digest_hours_raw) if digest_hours_raw else 0.0
    retention_hours_raw = os.getenv("RETENTION_SCAN_INTERVAL_HOURS", "").strip()
    retention_hours = float(retention_hours_raw) if retention_hours_raw else 0.0
    affiliate_minutes_raw = os.getenv("AFFILIATE_MONITOR_INTERVAL_MINUTES", "").strip()
    affiliate_minutes = float(affiliate_minutes_raw) if affiliate_minutes_raw else 0.0
    affiliate_privacy_hours_raw = os.getenv(
        "AFFILIATE_PRIVACY_RETENTION_INTERVAL_HOURS", ""
    ).strip()
    affiliate_privacy_hours = (
        float(affiliate_privacy_hours_raw) if affiliate_privacy_hours_raw else 0.0
    )
    referral_retry_minutes_raw = os.getenv(
        "REFERRAL_DELIVERY_RETRY_INTERVAL_MINUTES", "5"
    ).strip()
    referral_retry_minutes = (
        float(referral_retry_minutes_raw) if referral_retry_minutes_raw else 0.0
    )
    analytics_retention_hours_raw = os.getenv(
        "ANALYTICS_RETENTION_INTERVAL_HOURS", "24"
    ).strip()
    analytics_retention_hours = (
        float(analytics_retention_hours_raw) if analytics_retention_hours_raw else 0.0
    )
    analytics_quality_seconds_raw = os.getenv(
        "ANALYTICS_QUALITY_INTERVAL_SECONDS", "300"
    ).strip()
    analytics_quality_seconds = (
        max(60, int(analytics_quality_seconds_raw))
        if analytics_quality_seconds_raw
        else 0
    )
    tasks: list[asyncio.Task] = []

    from app.request_logging.config import RequestLogSettings
    from app.request_logging.writer import start_global_writer, stop_global_writer

    request_log_settings = RequestLogSettings.from_env()
    if request_log_settings.read_database_url:
        raise RuntimeError(
            "Public API must not receive LOG_READ_DATABASE_URL; "
            "deploy app.internal_admin separately"
        )
    if request_log_settings.enabled:
        from app.request_logging.crypto import PayloadCipher

        # Validate Fernet/HMAC material at startup instead of discovering a
        # malformed production secret on the first customer request.
        PayloadCipher(request_log_settings)
        request_log_writer = start_global_writer(request_log_settings)

        async def _request_log_retention_loop() -> None:
            while True:
                await asyncio.sleep(request_log_settings.retention_interval_hours * 3600)
                try:
                    result = await asyncio.to_thread(request_log_writer.maintain)
                    if any(result.values()):
                        logger.info("Request-log retention completed counts=%s", result)
                except Exception:
                    logger.exception("Request-log retention/rollup maintenance failed")

        tasks.append(asyncio.create_task(_request_log_retention_loop()))

    redis_url = (os.getenv("REDIS_URL") or "").strip()
    if redis_url:
        from app.services.local_worker_manager import (
            should_supervise_local_worker,
            start_local_worker,
            stop_local_worker,
        )

        if should_supervise_local_worker(redis_url):

            async def _local_worker_watchdog() -> None:
                owned_worker = None
                try:
                    while True:
                        if owned_worker is None or owned_worker.poll() is not None:
                            try:
                                owned_worker = await asyncio.to_thread(
                                    start_local_worker, redis_url
                                )
                                if owned_worker is not None:
                                    logger.info(
                                        "Started supervised local RQ worker (pid=%s)",
                                        owned_worker.pid,
                                    )
                            except Exception:
                                logger.exception("Could not start supervised local RQ worker")
                        await asyncio.sleep(15)
                finally:
                    await asyncio.to_thread(stop_local_worker, owned_worker)

            tasks.append(asyncio.create_task(_local_worker_watchdog()))

    if digest_hours > 0:

        async def _mention_digest_loop() -> None:
            while True:
                await asyncio.sleep(digest_hours * 3600)
                try:
                    from app.jobs.queue import enqueue_mention_digest_all_job

                    enqueue_mention_digest_all_job()
                except Exception:
                    logger.exception("Scheduled mention digest enqueue failed")

        tasks.append(asyncio.create_task(_mention_digest_loop()))

    if retention_hours > 0:

        async def _retention_loop() -> None:
            while True:
                await asyncio.sleep(retention_hours * 3600)
                db = SessionLocal()
                try:
                    from app.services.retention import enqueue_due_retention_projects
                    enqueue_due_retention_projects(db)
                except Exception:
                    logger.exception("Scheduled retention enqueue failed")
                finally:
                    db.close()

        tasks.append(asyncio.create_task(_retention_loop()))

    if affiliate_minutes > 0:

        async def _affiliate_monitor_loop() -> None:
            last_signature: tuple | None = None
            last_delivery = 0.0
            cooldown_minutes = max(
                5.0,
                float(os.getenv("AFFILIATE_ALERT_COOLDOWN_MINUTES", "60") or "60"),
            )
            while True:
                await asyncio.sleep(affiliate_minutes * 60)
                try:
                    from app.services.affiliate_reconciliation import (
                        database_reconciliation_report,
                        send_monitor_webhook,
                    )

                    def _build_report() -> dict:
                        monitor_db = SessionLocal()
                        try:
                            return database_reconciliation_report(monitor_db)
                        finally:
                            monitor_db.close()

                    report = await asyncio.to_thread(_build_report)
                    if report["status"] == "ok":
                        last_signature = None
                        continue
                    signature = tuple(
                        sorted((issue["code"], issue["severity"]) for issue in report["issues"])
                    )
                    now_monotonic = asyncio.get_running_loop().time()
                    should_deliver = (
                        signature != last_signature
                        or now_monotonic - last_delivery >= cooldown_minutes * 60
                    )
                    logger.warning(
                        "Affiliate reconciliation monitor status=%s issues=%s",
                        report["status"],
                        report["issue_counts"],
                    )
                    if should_deliver:
                        await asyncio.to_thread(send_monitor_webhook, report)
                        last_signature = signature
                        last_delivery = now_monotonic
                except Exception:
                    logger.exception("Scheduled affiliate reconciliation monitor failed")

        tasks.append(asyncio.create_task(_affiliate_monitor_loop()))

    if affiliate_privacy_hours > 0:

        async def _affiliate_privacy_loop() -> None:
            while True:
                try:
                    from app.services.affiliate_privacy import (
                        apply_affiliate_privacy_retention,
                    )

                    privacy_db = SessionLocal()
                    try:
                        result = await asyncio.to_thread(
                            apply_affiliate_privacy_retention,
                            privacy_db,
                        )
                    finally:
                        privacy_db.close()
                    if any(result.values()):
                        logger.info("Affiliate privacy retention completed counts=%s", result)
                except Exception:
                    logger.exception("Scheduled affiliate privacy retention failed")
                await asyncio.sleep(affiliate_privacy_hours * 3600)

        tasks.append(asyncio.create_task(_affiliate_privacy_loop()))

    if referral_retry_minutes > 0:

        async def _referral_delivery_retry_loop() -> None:
            while True:
                await asyncio.sleep(referral_retry_minutes * 60)
                retry_db = SessionLocal()
                try:
                    from app.services.referrals import retry_failed_invite_deliveries

                    result = await asyncio.to_thread(
                        retry_failed_invite_deliveries,
                        retry_db,
                    )
                    if result["attempted"]:
                        logger.info("Referral delivery retries completed counts=%s", result)
                except Exception:
                    logger.exception("Scheduled referral delivery retry failed")
                finally:
                    retry_db.close()

        tasks.append(asyncio.create_task(_referral_delivery_retry_loop()))

    if analytics_retention_hours > 0:

        async def _analytics_retention_loop() -> None:
            while True:
                # Do not block application startup/shutdown on a database sweep;
                # deploy migrations first, then run at the configured cadence.
                await asyncio.sleep(analytics_retention_hours * 3600)
                try:
                    from app.services.analytics_retention import apply_analytics_retention

                    retention_db = SessionLocal()
                    try:
                        result = await asyncio.to_thread(
                            apply_analytics_retention,
                            retention_db,
                        )
                    finally:
                        retention_db.close()
                    if any(result.values()):
                        logger.info("Analytics retention completed counts=%s", result)
                except Exception:
                    logger.exception("Analytics retention sweep failed")

        tasks.append(asyncio.create_task(_analytics_retention_loop()))

    analytics_interval_raw = os.getenv("ANALYTICS_DELIVERY_INTERVAL_SECONDS", "30").strip()
    analytics_interval = max(10, int(analytics_interval_raw or "30"))
    if redis_url and os.getenv("POSTHOG_PROJECT_API_KEY", "").strip():

        async def _analytics_delivery_loop() -> None:
            while True:
                try:
                    from app.jobs.queue import enqueue_analytics_delivery_job

                    enqueue_analytics_delivery_job()
                except Exception:
                    logger.exception("Scheduled analytics delivery enqueue failed")
                await asyncio.sleep(analytics_interval)

        tasks.append(asyncio.create_task(_analytics_delivery_loop()))

    if redis_url and os.getenv("POSTHOG_PROJECT_ID", "").strip():

        async def _analytics_privacy_request_loop() -> None:
            while True:
                try:
                    from app.jobs.queue import enqueue_analytics_privacy_job

                    enqueue_analytics_privacy_job()
                except Exception:
                    logger.exception("Scheduled analytics privacy enqueue failed")
                await asyncio.sleep(max(60, analytics_interval))

        tasks.append(asyncio.create_task(_analytics_privacy_request_loop()))

    if analytics_quality_seconds > 0:

        async def _analytics_quality_loop() -> None:
            while True:
                await asyncio.sleep(analytics_quality_seconds)
                try:
                    from app.jobs.analytics_quality import analytics_quality_job

                    await asyncio.to_thread(analytics_quality_job)
                except Exception:
                    logger.exception("Scheduled analytics quality monitor failed")

        tasks.append(asyncio.create_task(_analytics_quality_loop()))

    yield

    for task in tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    stop_global_writer()


app = FastAPI(
    title="Editube API",
    description=(
        "Backend API for Editube, powering authentication, projects, uploads, "
        "video collaboration, comments, notifications, and analytics."
    ),
    version="1.0.0",
    contact={
        "name": "Editube Team",
    },
    lifespan=_app_lifespan,
)


# Explicit production / known hosts; local dev also covered by allow_origin_regex below.
origins = [
    "http://localhost:3000",
    "http://localhost:3001",
    "http://localhost:3002",
    "http://localhost:3003",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:3001",
    "http://127.0.0.1:3002",
    "http://127.0.0.1:3003",
    "https://editube-kemojals-projects.vercel.app",
]
_extra = os.getenv("CORS_ORIGINS", "")
if _extra.strip():
    origins.extend(o.strip() for o in _extra.split(",") if o.strip())

# Browsers may use 127.0.0.1 or [::1] even when the bar says "localhost"; any dev port.
_local_origin_regex = r"^http://(localhost|127\.0\.0\.1|\[::1\]):\d+$"
_local_origin_pattern = re.compile(_local_origin_regex)


def _cors_headers_for_request(request: Request) -> dict[str, str]:
    """Mirror CORSMiddleware so error responses still expose CORS (avoids misleading browser CORS noise on 500)."""
    origin = request.headers.get("origin")
    if not origin:
        return {}
    if origin not in origins and not _local_origin_pattern.fullmatch(origin):
        return {}
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Credentials": "true",
    }


app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_origin_regex=_local_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)


_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
_TRACE_ID_RE = re.compile(r"^[a-fA-F0-9]{16,32}$")


@app.middleware("http")
async def request_correlation_and_failure_analytics(request: Request, call_next):
    incoming_request_id = (request.headers.get("x-request-id") or "").strip()
    request_id = (
        incoming_request_id
        if _CORRELATION_ID_RE.fullmatch(incoming_request_id)
        else str(uuid.uuid4())
    )
    sentry_trace = (request.headers.get("sentry-trace") or "").split("-", 1)[0]
    trace_id = sentry_trace if _TRACE_ID_RE.fullmatch(sentry_trace) else None
    analytics_session_id = (request.headers.get("x-analytics-session-id") or "").strip()
    if not _CORRELATION_ID_RE.fullmatch(analytics_session_id):
        analytics_session_id = None

    context_token = begin_request(
        request_id=request_id,
        trace_id=trace_id,
        analytics_session_id=analytics_session_id,
    )
    request.state.request_id = request_id
    started = time.perf_counter()
    try:
        response = await call_next(request)
        route = request.scope.get("route")
        route_template = getattr(route, "path", None)
        bind_route(route_template)
        duration_ms = int((time.perf_counter() - started) * 1000)
        if (
            (response.status_code >= 500 or response.status_code == 429)
            and not getattr(request.state, "failure_analytics_emitted", False)
        ):
            emit_after_commit(
                "api_request_failed",
                properties={
                    "method": request.method,
                    "status_class": f"{response.status_code // 100}xx",
                    "duration_ms": duration_ms,
                    "error_code": f"http_{response.status_code}",
                    "result": "failure",
                },
            )
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        end_request(context_token)

# Storage backends (R2 / Cloudinary / local) are configured lazily by app.storage;
# no global cloudinary.config() needed here. See docs/r2-storage-migration-plan.md.

app.include_router(api_router)

# Serve rendered clips (and other local uploads) in dev so the frontend can play
# and download the mp4 produced by the clip render worker.
from fastapi.staticfiles import StaticFiles  # noqa: E402

_uploads_dir = os.path.abspath(os.environ.get("UPLOADS_DIR", "./uploads"))
os.makedirs(_uploads_dir, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=_uploads_dir), name="uploads")


@app.middleware("http")
async def uploads_media_cors_fallback(request: Request, call_next):
    """
    StaticFiles responses sometimes omit CORS headers when the browser sends no `Origin`
    (common for `<video src>`). Mirror CORSMiddleware for known dev origins, else `*` so
    cross-port localhost playback (e.g. Next on :3002, API on :8000) still works.
    """
    response = await call_next(request)
    if not request.url.path.startswith("/uploads"):
        return response
    if response.headers.get("access-control-allow-origin"):
        return response
    origin = request.headers.get("origin")
    if origin and (origin in origins or _local_origin_pattern.fullmatch(origin)):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
    elif request.method in ("GET", "HEAD", "OPTIONS"):
        response.headers["Access-Control-Allow-Origin"] = "*"
    return response


# Innermost user middleware: downstream dependency ContextVar mutations remain
# visible here, while the outer correlation middleware still supplies the
# request ID. WebSocket scopes are passed through untouched.
from app.request_logging.middleware import RequestLoggingMiddleware  # noqa: E402

app.add_middleware(RequestLoggingMiddleware)


# Include the WebSocket app
# app.mount("/", websocket_app)


@app.websocket("/ws/notifications")
async def notifications_websocket(websocket: WebSocket):
    token = websocket.query_params.get("token")
    if not token:
        auth_header = websocket.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header.split(" ", 1)[1].strip()

    if not token:
        await websocket.close(code=1008)
        return

    db = SessionLocal()
    try:
        user = authenticate_access_token(db, token, touch_session=True)
    except Exception:
        db.close()
        await websocket.close(code=1008)
        return

    user_id = user.id
    db.close()
    await notifications_ws_manager.connect(user_id, websocket)

    try:
        while True:
            # Heartbeat; no-op payload keeps connection alive.
            await websocket.receive_text()
    except WebSocketDisconnect:
        notifications_ws_manager.disconnect(user_id, websocket)
    except Exception:
        notifications_ws_manager.disconnect(user_id, websocket)


@app.websocket("/ws/review-room/{token}")
async def review_room_websocket(websocket: WebSocket, token: str):
    if os.getenv("FEATURE_REALTIME_PRESENCE", "1").strip() in ("0", "false", "False"):
        await websocket.close(code=1008)
        return
    session_id_raw = websocket.query_params.get("session_id")
    if not session_id_raw:
        await websocket.close(code=1008)
        return
    try:
        session_id = int(session_id_raw)
    except ValueError:
        await websocket.close(code=1008)
        return
    last_seq_raw = websocket.query_params.get("last_seq", "0")
    try:
        last_seq = int(last_seq_raw or "0")
    except ValueError:
        last_seq = 0

    db = SessionLocal()
    try:
        link = db.query(ReviewLink).filter(ReviewLink.token == token).first()
        if not link:
            await websocket.close(code=1008)
            return
        session = (
            db.query(ReviewSession)
            .filter(
                ReviewSession.id == session_id,
                ReviewSession.review_link_id == link.id,
            )
            .first()
        )
        if not session:
            await websocket.close(code=1008)
            return
        room_id = f"review:{link.id}"
        await review_room_ws_manager.connect(room_id, session_id, websocket)
        video = db.query(Video).filter(Video.id == link.video_id).first()
        project = db.query(Project).filter(Project.id == video.project_id).first() if video else None
        if project is not None:
            analytics_room_id = f"review-link:{link.id}:session:{session.id}"
            common = {
                "workspace_id": project.workspace_id,
                "source": "review_service",
                "properties": {
                    "project_id": project.id,
                    "video_id": video.id,
                    "review_link_id": link.id,
                    "review_session_id": session.id,
                    "feature_key": "live_review_room",
                    "actor_type": "guest",
                    "result": "success",
                },
            }
            emit_once(
                db,
                "review_live_room_joined",
                event_id=f"{analytics_room_id}:joined",
                **common,
            )
            emit_once(
                db,
                "feature_started",
                event_id=f"feature:live-review-room:{analytics_room_id}:started",
                **common,
            )
            db.commit()
        base_presence = {
            "session_id": session.id,
            "guest_name": session.guest_name or "Guest",
            "guest_avatar_url": session.guest_avatar_url,
            "playhead": 0,
            "cursor_x": None,
            "cursor_y": None,
            "is_playing": False,
        }
        presence = review_room_ws_manager.upsert_presence(room_id, session_id, base_presence)
        await review_room_ws_manager.broadcast(
            room_id,
            review_room_ws_manager.add_event(
                room_id, {"event": "presence.snapshot", "payload": presence}
            ),
        )
        replay_events = review_room_ws_manager.replay_since(room_id, last_seq)
        for item in replay_events[-200:]:
            await websocket.send_json(item)
        await websocket.send_json(
            review_room_ws_manager.add_event(
                room_id,
                {
                    "event": "room.controls",
                    "payload": review_room_ws_manager.controls_state(room_id),
                },
            )
        )
        history_rows = (
            db.query(ReviewRoomMessage)
            .filter(ReviewRoomMessage.review_link_id == link.id)
            .order_by(ReviewRoomMessage.created_at.desc())
            .limit(50)
            .all()
        )
        history_payload = [
            {
                "id": row.id,
                "session_id": row.session_id,
                "body": row.body,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in reversed(history_rows)
        ]
        await websocket.send_json({"event": "chat.history", "payload": history_payload})

        while True:
            msg = await websocket.receive_json()
            event = (msg.get("event") or "").strip()
            payload = msg.get("payload") or {}
            if review_room_ws_manager.is_muted(room_id, session.id) and event in (
                "chat.message",
                "playback.sync",
                "host.control",
            ):
                continue
            if event == "presence.update":
                merged = {
                    **base_presence,
                    **payload,
                    "session_id": session.id,
                    "guest_name": session.guest_name or "Guest",
                    "guest_avatar_url": session.guest_avatar_url,
                }
                snapshot = review_room_ws_manager.upsert_presence(room_id, session_id, merged)
                await review_room_ws_manager.broadcast(
                    room_id,
                    review_room_ws_manager.add_event(
                        room_id,
                        {
                            "event": "presence.snapshot",
                            "payload": snapshot,
                        },
                    ),
                )
            elif event == "presence.heartbeat":
                snapshot = review_room_ws_manager.heartbeat(room_id, session_id)
                await review_room_ws_manager.broadcast(
                    room_id,
                    review_room_ws_manager.add_event(
                        room_id, {"event": "presence.snapshot", "payload": snapshot}
                    ),
                )
            elif event in ("playback.sync", "host.control"):
                controls = review_room_ws_manager.controls_state(room_id)
                if controls.get("locked") and controls.get("host_session_id") not in (
                    None,
                    session.id,
                ):
                    continue
                await review_room_ws_manager.broadcast(
                    room_id,
                    review_room_ws_manager.add_event(
                        room_id,
                        {
                            "event": event,
                            "payload": {
                                **payload,
                                "session_id": session.id,
                                "guest_name": session.guest_name or "Guest",
                            },
                        },
                    ),
                )
                if project is not None:
                    emit_once(
                        db,
                        "feature_result_used",
                        event_id=f"feature:live-review-room:{analytics_room_id}:result-used",
                        workspace_id=project.workspace_id,
                        source="review_service",
                        properties={
                            "project_id": project.id,
                            "video_id": video.id,
                            "review_link_id": link.id,
                            "review_session_id": session.id,
                            "feature_key": "live_review_room",
                            "result_type": "playback_sync",
                            "actor_type": "guest",
                            "result": "success",
                        },
                    )
                    db.commit()
            elif event == "chat.message":
                body = (payload.get("body") or "").strip()
                if not body:
                    continue
                row = ReviewRoomMessage(
                    review_link_id=link.id,
                    session_id=session.id,
                    body=body[:5000],
                )
                db.add(row)
                db.commit()
                db.refresh(row)
                await review_room_ws_manager.broadcast(
                    room_id,
                    review_room_ws_manager.add_event(
                        room_id,
                        {
                            "event": "chat.message",
                            "payload": {
                                "id": row.id,
                                "session_id": session.id,
                                "guest_name": session.guest_name or "Guest",
                                "guest_avatar_url": session.guest_avatar_url,
                                "body": row.body,
                                "created_at": row.created_at.isoformat() if row.created_at else None,
                            },
                        },
                    ),
                )
            elif event == "moderation.lock":
                controls = review_room_ws_manager.controls_state(room_id)
                existing_host = controls.get("host_session_id")
                if existing_host is not None and existing_host != session.id:
                    continue
                state = review_room_ws_manager.set_lock(
                    room_id,
                    bool(payload.get("locked", False)),
                    (
                        session.id
                        if bool(payload.get("locked", False))
                        else (existing_host if existing_host == session.id else None)
                    ),
                )
                await review_room_ws_manager.broadcast(
                    room_id,
                    review_room_ws_manager.add_event(
                        room_id, {"event": "room.controls", "payload": state}
                    ),
                )
            elif event == "moderation.mute":
                controls = review_room_ws_manager.controls_state(room_id)
                host_session_id = controls.get("host_session_id")
                if host_session_id is not None and host_session_id != session.id:
                    continue
                target_session_id = int(payload.get("target_session_id") or 0)
                if target_session_id > 0:
                    state = review_room_ws_manager.mute_session(
                        room_id,
                        target_session_id=target_session_id,
                        muted=bool(payload.get("muted", True)),
                    )
                    await review_room_ws_manager.broadcast(
                        room_id,
                        review_room_ws_manager.add_event(
                            room_id, {"event": "room.controls", "payload": state}
                        ),
                    )
            elif event == "moderation.remove":
                controls = review_room_ws_manager.controls_state(room_id)
                host_session_id = controls.get("host_session_id")
                if host_session_id is not None and host_session_id != session.id:
                    continue
                target_session_id = int(payload.get("target_session_id") or 0)
                if target_session_id > 0 and target_session_id != session.id:
                    if host_session_id is not None and target_session_id == host_session_id:
                        continue
                    await review_room_ws_manager.close_session(
                        room_id, target_session_id, code=4001
                    )
    except WebSocketDisconnect:
        if "room_id" in locals():
            snapshot = review_room_ws_manager.remove_presence(room_id, session_id)
            review_room_ws_manager.disconnect(room_id, session_id, websocket)
            await review_room_ws_manager.broadcast(
                room_id, {"event": "presence.snapshot", "payload": snapshot}
            )
    except Exception:
        if "room_id" in locals():
            snapshot = review_room_ws_manager.remove_presence(room_id, session_id)
            review_room_ws_manager.disconnect(room_id, session_id, websocket)
            await review_room_ws_manager.broadcast(
                room_id, {"event": "presence.snapshot", "payload": snapshot}
            )
    finally:
        db.close()


@app.websocket("/ws/team-video/{video_id}")
async def team_video_websocket(websocket: WebSocket, video_id: int):
    token = websocket.query_params.get("token")
    if not token:
        auth_header = websocket.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header.split(" ", 1)[1].strip()
    if not token:
        await websocket.close(code=1008)
        return

    last_seq_raw = websocket.query_params.get("last_seq", "0")
    try:
        last_seq = int(last_seq_raw or "0")
    except ValueError:
        last_seq = 0

    db = SessionLocal()
    try:
        user = authenticate_access_token(db, token, touch_session=True)
    except Exception:
        db.close()
        await websocket.close(code=1008)
        return

    try:
        video = db.query(Video).filter(Video.id == video_id).first()
        if not video:
            await websocket.close(code=1008)
            return
        project = db.query(Project).filter(Project.id == video.project_id).first()
        if not project or not can_access_project(db, user.id, project):
            await websocket.close(code=1008)
            return
        room_id = f"team-video:{video_id}"
        await review_room_ws_manager.connect(room_id, user.id, websocket)
        analytics_connection_id = uuid.uuid4().hex
        common = {
            "user": user,
            "workspace_id": project.workspace_id,
            "properties": {
                "project_id": project.id,
                "video_id": video.id,
                "feature_key": "live_review_room",
                "actor_type": "member",
                "result": "success",
            },
        }
        emit_once(
            db,
            "review_live_room_joined",
            event_id=f"team-live-room:{video.id}:{analytics_connection_id}:joined",
            **common,
        )
        emit_once(
            db,
            "feature_started",
            event_id=f"feature:live-review-room:{video.id}:{analytics_connection_id}:started",
            **common,
        )
        db.commit()
        base_presence = {
            "session_id": user.id,
            "user_id": user.id,
            "guest_name": user.name or user.email or f"User {user.id}",
            "guest_avatar_url": getattr(user, "avatar_url", None),
            "playhead": 0,
            "cursor_x": None,
            "cursor_y": None,
            "is_playing": False,
        }
        presence = review_room_ws_manager.upsert_presence(room_id, user.id, base_presence)
        await review_room_ws_manager.broadcast(
            room_id,
            review_room_ws_manager.add_event(
                room_id, {"event": "presence.snapshot", "payload": presence}
            ),
        )
        for item in review_room_ws_manager.replay_since(room_id, last_seq)[-200:]:
            await websocket.send_json(item)

        await websocket.send_json(
            review_room_ws_manager.add_event(
                room_id,
                {
                    "event": "room.controls",
                    "payload": review_room_ws_manager.controls_state(room_id),
                },
            )
        )
        history_rows = (
            db.query(TeamVideoRoomMessage)
            .options(joinedload(TeamVideoRoomMessage.user))
            .filter(TeamVideoRoomMessage.video_id == video_id)
            .order_by(TeamVideoRoomMessage.created_at.desc())
            .limit(50)
            .all()
        )
        history_payload = []
        for row in reversed(history_rows):
            author = row.user
            guest_name = (
                (author.name or author.email or f"User {row.user_id}") if author else f"User {row.user_id}"
            )
            guest_avatar_url = getattr(author, "avatar_url", None) if author else None
            history_payload.append(
                {
                    "id": row.id,
                    "user_id": row.user_id,
                    "guest_name": guest_name,
                    "guest_avatar_url": guest_avatar_url,
                    "body": row.body,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
            )
        await websocket.send_json({"event": "chat.history", "payload": history_payload})

        can_mod = can_moderate_video_comments(db, project, user.id)

        while True:
            msg = await websocket.receive_json()
            event = (msg.get("event") or "").strip()
            payload = msg.get("payload") or {}
            if review_room_ws_manager.is_muted(room_id, user.id) and event in (
                "chat.message",
                "playback.sync",
                "host.control",
            ):
                continue
            if event == "presence.update":
                merged = {
                    **base_presence,
                    **payload,
                    "session_id": user.id,
                    "user_id": user.id,
                    "guest_name": user.name or user.email or f"User {user.id}",
                    "guest_avatar_url": getattr(user, "avatar_url", None),
                }
                snapshot = review_room_ws_manager.upsert_presence(room_id, user.id, merged)
                await review_room_ws_manager.broadcast(
                    room_id,
                    review_room_ws_manager.add_event(
                        room_id, {"event": "presence.snapshot", "payload": snapshot}
                    ),
                )
            elif event == "presence.heartbeat":
                snapshot = review_room_ws_manager.heartbeat(room_id, user.id)
                await review_room_ws_manager.broadcast(
                    room_id,
                    review_room_ws_manager.add_event(
                        room_id, {"event": "presence.snapshot", "payload": snapshot}
                    ),
                )
            elif event in ("playback.sync", "host.control"):
                controls = review_room_ws_manager.controls_state(room_id)
                if controls.get("locked") and controls.get("host_session_id") not in (
                    None,
                    user.id,
                ):
                    continue
                await review_room_ws_manager.broadcast(
                    room_id,
                    review_room_ws_manager.add_event(
                        room_id,
                        {
                            "event": event,
                            "payload": {
                                **payload,
                                "session_id": user.id,
                                "guest_name": user.name or user.email or f"User {user.id}",
                            },
                        },
                    ),
                )
                emit_once(
                    db,
                    "feature_result_used",
                    event_id=f"feature:live-review-room:{video.id}:{analytics_connection_id}:result-used",
                    user=user,
                    workspace_id=project.workspace_id,
                    properties={
                        "project_id": project.id,
                        "video_id": video.id,
                        "feature_key": "live_review_room",
                        "result_type": "playback_sync",
                        "actor_type": "member",
                        "result": "success",
                    },
                )
                db.commit()
            elif event == "chat.message":
                body = (payload.get("body") or "").strip()
                if not body:
                    continue
                row = TeamVideoRoomMessage(
                    video_id=video_id,
                    user_id=user.id,
                    body=body[:5000],
                )
                db.add(row)
                db.commit()
                db.refresh(row)
                await review_room_ws_manager.broadcast(
                    room_id,
                    review_room_ws_manager.add_event(
                        room_id,
                        {
                            "event": "chat.message",
                            "payload": {
                                "id": row.id,
                                "user_id": user.id,
                                "guest_name": user.name or user.email or f"User {user.id}",
                                "guest_avatar_url": getattr(user, "avatar_url", None),
                                "body": row.body,
                                "created_at": row.created_at.isoformat() if row.created_at else None,
                            },
                        },
                    ),
                )
            elif event == "moderation.lock":
                if not can_mod:
                    continue
                controls = review_room_ws_manager.controls_state(room_id)
                existing_host = controls.get("host_session_id")
                if existing_host is not None and existing_host != user.id:
                    continue
                state = review_room_ws_manager.set_lock(
                    room_id,
                    bool(payload.get("locked", False)),
                    (
                        user.id
                        if bool(payload.get("locked", False))
                        else (existing_host if existing_host == user.id else None)
                    ),
                )
                await review_room_ws_manager.broadcast(
                    room_id,
                    review_room_ws_manager.add_event(
                        room_id, {"event": "room.controls", "payload": state}
                    ),
                )
            elif event == "moderation.mute":
                if not can_mod:
                    continue
                controls = review_room_ws_manager.controls_state(room_id)
                host_session_id = controls.get("host_session_id")
                if host_session_id is not None and host_session_id != user.id:
                    continue
                target_session_id = int(payload.get("target_session_id") or 0)
                if target_session_id > 0:
                    state = review_room_ws_manager.mute_session(
                        room_id,
                        target_session_id=target_session_id,
                        muted=bool(payload.get("muted", True)),
                    )
                    await review_room_ws_manager.broadcast(
                        room_id,
                        review_room_ws_manager.add_event(
                            room_id, {"event": "room.controls", "payload": state}
                        ),
                    )
            elif event == "moderation.remove":
                if not can_mod:
                    continue
                controls = review_room_ws_manager.controls_state(room_id)
                host_session_id = controls.get("host_session_id")
                if host_session_id is not None and host_session_id != user.id:
                    continue
                target_session_id = int(payload.get("target_session_id") or 0)
                if target_session_id > 0 and target_session_id != user.id:
                    if host_session_id is not None and target_session_id == host_session_id:
                        continue
                    await review_room_ws_manager.close_session(
                        room_id, target_session_id, code=4001
                    )
            elif event in ("comment.signal", "typing.signal"):
                await review_room_ws_manager.broadcast(
                    room_id,
                    review_room_ws_manager.add_event(
                        room_id,
                        {
                            "event": event,
                            "payload": {
                                **payload,
                                "user_id": user.id,
                                "name": user.name or user.email or f"User {user.id}",
                                "avatar_url": getattr(user, "avatar_url", None),
                            },
                        },
                    ),
                )
    except WebSocketDisconnect:
        if "room_id" in locals():
            snapshot = review_room_ws_manager.remove_presence(room_id, user.id)
            review_room_ws_manager.disconnect(room_id, user.id, websocket)
            await review_room_ws_manager.broadcast(
                room_id,
                review_room_ws_manager.add_event(
                    room_id, {"event": "presence.snapshot", "payload": snapshot}
                ),
            )
    except Exception:
        if "room_id" in locals():
            snapshot = review_room_ws_manager.remove_presence(room_id, user.id)
            review_room_ws_manager.disconnect(room_id, user.id, websocket)
            await review_room_ws_manager.broadcast(
                room_id,
                review_room_ws_manager.add_event(
                    room_id, {"event": "presence.snapshot", "payload": snapshot}
                ),
            )
    finally:
        db.close()


@app.websocket("/ws/clip-room/{clip_id}")
async def clip_room_websocket(websocket: WebSocket, clip_id: int):
    token = websocket.query_params.get("token")
    if not token:
        auth_header = websocket.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header.split(" ", 1)[1].strip()
    if not token:
        await websocket.close(code=1008)
        return

    session_id_raw = websocket.query_params.get("session_id")
    if not session_id_raw:
        await websocket.close(code=1008)
        return
    try:
        session_id = int(session_id_raw)
    except ValueError:
        await websocket.close(code=1008)
        return

    last_seq_raw = websocket.query_params.get("last_seq", "0")
    try:
        last_seq = int(last_seq_raw or "0")
    except ValueError:
        last_seq = 0

    db = SessionLocal()
    try:
        user = authenticate_access_token(db, token, touch_session=True)
    except Exception:
        db.close()
        await websocket.close(code=1008)
        return

    try:
        clip = db.query(Clip).filter(Clip.id == clip_id).first()
        if not clip:
            await websocket.close(code=1008)
            return
        video = db.query(Video).filter(Video.id == clip.video_id).first()
        if not video:
            await websocket.close(code=1008)
            return
        project = db.query(Project).filter(Project.id == video.project_id).first()
        if not project or not can_access_project(db, user.id, project):
            await websocket.close(code=1008)
            return

        room_id = f"clip-room:{clip_id}"
        await review_room_ws_manager.connect(room_id, session_id, websocket)
        base_presence = {
            "session_id": session_id,
            "user_id": user.id,
            "guest_name": user.full_name or user.name or user.email or f"User {user.id}",
            "guest_avatar_url": getattr(user, "avatar_url", None),
            "cursor_x": None,
            "cursor_y": None,
            "active_range_label": None,
            "color": None,
        }
        presence = review_room_ws_manager.upsert_presence(room_id, session_id, base_presence)
        await review_room_ws_manager.broadcast(
            room_id,
            review_room_ws_manager.add_event(
                room_id, {"event": "presence.snapshot", "payload": presence}
            ),
        )
        for item in review_room_ws_manager.replay_since(room_id, last_seq)[-200:]:
            await websocket.send_json(item)

        while True:
            msg = await websocket.receive_json()
            event = (msg.get("event") or "").strip()
            payload = msg.get("payload") or {}
            if event == "presence.update":
                merged = {
                    **base_presence,
                    **payload,
                    "session_id": session_id,
                    "user_id": user.id,
                    "guest_name": user.full_name or user.name or user.email or f"User {user.id}",
                    "guest_avatar_url": getattr(user, "avatar_url", None),
                }
                snapshot = review_room_ws_manager.upsert_presence(room_id, session_id, merged)
                await review_room_ws_manager.broadcast(
                    room_id,
                    review_room_ws_manager.add_event(
                        room_id, {"event": "presence.snapshot", "payload": snapshot}
                    ),
                )
            elif event == "presence.heartbeat":
                snapshot = review_room_ws_manager.heartbeat(room_id, session_id)
                await review_room_ws_manager.broadcast(
                    room_id,
                    review_room_ws_manager.add_event(
                        room_id, {"event": "presence.snapshot", "payload": snapshot}
                    ),
                )
    except WebSocketDisconnect:
        if "room_id" in locals():
            snapshot = review_room_ws_manager.remove_presence(room_id, session_id)
            review_room_ws_manager.disconnect(room_id, session_id, websocket)
            await review_room_ws_manager.broadcast(
                room_id,
                review_room_ws_manager.add_event(
                    room_id, {"event": "presence.snapshot", "payload": snapshot}
                ),
            )
    except Exception:
        if "room_id" in locals():
            snapshot = review_room_ws_manager.remove_presence(room_id, session_id)
            review_room_ws_manager.disconnect(room_id, session_id, websocket)
            await review_room_ws_manager.broadcast(
                room_id,
                review_room_ws_manager.add_event(
                    room_id, {"event": "presence.snapshot", "payload": snapshot}
                ),
            )
    finally:
        db.close()


@app.get("/health")
async def health():
    """Liveness probe for reverse proxies and Dokploy."""
    return {"status": "ok"}


@app.get("/health/request-logging")
def request_logging_health():
    """Sanitized public-process writer health for infrastructure monitoring."""
    from app.request_logging.config import RequestLogSettings
    from app.request_logging.writer import get_global_writer

    settings = RequestLogSettings.from_env()
    if not settings.enabled:
        return {"status": "disabled", "enabled": False}
    snapshot = get_global_writer(settings).health()
    degraded = (
        not snapshot["thread_alive"]
        or snapshot["dropped"] > 0
        or snapshot["failed"] > 0
        or snapshot["queue_depth"] >= int(snapshot["queue_capacity"] * 0.8)
    )
    if degraded:
        _record_dependency_degraded(
            "request_log_writer",
            "degraded",
            "request_log_writer_degraded",
        )
    return JSONResponse(
        {
            "status": "degraded" if degraded else "ok",
            "enabled": True,
            "thread_alive": snapshot["thread_alive"],
            "queue_depth": snapshot["queue_depth"],
            "queue_capacity": snapshot["queue_capacity"],
            "enqueued": snapshot["enqueued"],
            "written": snapshot["written"],
            "payloads_shed": snapshot["payloads_shed"],
            "dropped": snapshot["dropped"],
            "failed": snapshot["failed"],
            "error_present": bool(snapshot["last_error"]),
        },
        status_code=503 if degraded else 200,
    )


@app.get("/health/affiliate")
def affiliate_health(db: Session = Depends(get_db)):
    """Sanitized affiliate-accounting health for Dokploy or uptime monitors."""
    try:
        from app.services.affiliate_reconciliation import (
            cached_database_reconciliation_report,
        )

        report = cached_database_reconciliation_report(db)
        payload = {
            "status": report["status"],
            "generated_at": report["generated_at"],
            "checked": report["checked"],
            "issue_counts": report["issue_counts"],
        }
        return JSONResponse(
            payload,
            status_code=503 if report["status"] == "critical" else 200,
        )
    except Exception:
        logger.exception("Affiliate health check failed")
        _record_dependency_degraded(
            "affiliate_reconciliation",
            "down",
            "affiliate_health_unavailable",
        )
        return JSONResponse(
            {"status": "critical", "error": "affiliate_health_unavailable"},
            status_code=503,
        )


@app.get("/health/queue")
async def queue_health():
    """
    Queue health snapshot for UI and CLI checks.
    Reports Redis connectivity, worker presence on the default queue, and backlog size.
    """
    result = {
        "status": "degraded",
        "redis_reachable": False,
        "worker_connected": False,
        "worker_count": 0,
        "queue_backlog_count": 0,
        "queue_name": "default",
        "error": None,
    }

    redis_url = (os.getenv("REDIS_URL") or "").strip()
    if not redis_url:
        result["error"] = "REDIS_URL not configured"
        _record_dependency_degraded("queue", "degraded", "redis_not_configured")
        return result

    try:
        from redis import Redis
        from rq import Queue, Worker

        conn = Redis.from_url(redis_url)
        conn.ping()
        result["redis_reachable"] = True

        queue = Queue("default", connection=conn)
        result["queue_backlog_count"] = int(queue.count)

        workers = Worker.all(connection=conn)
        default_workers = sum(
            1 for worker in workers if any(q.name == "default" for q in worker.queues)
        )
        result["worker_count"] = default_workers
        result["worker_connected"] = default_workers > 0
        result["status"] = "ok" if result["worker_connected"] else "degraded"
        if not result["worker_connected"]:
            _record_dependency_degraded("queue", "degraded", "worker_not_connected")
    except Exception as exc:  # noqa: BLE001
        result["status"] = "down"
        result["error"] = str(exc)[:300]
        _record_dependency_degraded("queue", "down", "redis_unreachable")

    return result


@app.get("/")
async def read_root():
    """
    Backend root. If you are seeing this in a browser, you are likely hitting the API 
    port (8000) instead of the frontend port (3000/3002).
    """
    return {
        "name": "Editube API",
        "status": "online",
        "frontend_url": os.getenv("FRONTEND_BASE_URL", "http://localhost:3000"),
        "message": "Welcome to Editube API. Use /docs for API documentation."
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)





@app.exception_handler(Exception)
async def unicorn_exception_handler(request: Request, exc: Exception):
    route = request.scope.get("route")
    bind_route(getattr(route, "path", None))
    context = current_request_context()
    logger.error(
        "Unhandled request error request_id=%s route=%s",
        context.request_id,
        context.route_template,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    capture_exception(exc)
    request.state.failure_analytics_emitted = True
    emit_after_commit(
        "api_request_failed",
        properties={
            "method": request.method,
            "status_class": "5xx",
            "error_code": "unhandled_exception",
            "result": "failure",
        },
    )
    return JSONResponse(
        status_code=500,
        content={"message": "Internal server error"},
        headers={
            **_cors_headers_for_request(request),
            "X-Request-ID": context.request_id or str(uuid.uuid4()),
        },
    )
