import asyncio
import cloudinary
import logging
import os
import re
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import api_router
from .db.database import SessionLocal
from .utils.security import authenticate_access_token
from .websocket_manager import notifications_ws_manager, review_room_ws_manager
from sqlalchemy.orm import joinedload

from .db.models import (
    ReviewLink,
    ReviewSession,
    ReviewRoomMessage,
    TeamVideoRoomMessage,
    Video,
    Project,
)
from .services.project_access import can_access_project, can_moderate_video_comments

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


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
    tasks: list[asyncio.Task] = []
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

    yield

    for task in tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


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
)

# cloudinary.config( 
#   cloud_name = "dtpnbesbx", 
#   api_key = "811133693665998", 
#   api_secret = "1YJOBmJ9LN1Aqhyc8AlUoAOHF9A" 
# )
cloudinary.config( 
  cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME"), 
  api_key = os.getenv("CLOUDINARY_API_KEY"), 
  api_secret = os.getenv("CLOUDINARY_API_SECRET") 
)

app.include_router(api_router)

# Serve rendered clips (and other local uploads) in dev so the frontend can play
# and download the mp4 produced by the clip render worker.
from fastapi.staticfiles import StaticFiles  # noqa: E402

_uploads_dir = os.path.abspath(os.environ.get("UPLOADS_DIR", "./uploads"))
os.makedirs(_uploads_dir, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=_uploads_dir), name="uploads")

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


@app.get("/health")
async def health():
    """Liveness probe for reverse proxies and Dokploy."""
    return {"status": "ok"}


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
    except Exception as exc:  # noqa: BLE001
        result["status"] = "down"
        result["error"] = str(exc)[:300]

    return result


@app.get("/")
async def read_item():
    return {"hello word"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)





@app.exception_handler(Exception)
async def unicorn_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled error: {exc}")
    return JSONResponse(
        status_code=500,
        content={"message": "Internal server error"},
        headers=_cors_headers_for_request(request),
    )