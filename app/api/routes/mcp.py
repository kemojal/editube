"""Model Context Protocol (MCP) server endpoint.

Implements the JSON-RPC 2.0 methods an MCP client needs over the Streamable HTTP
transport (``initialize``, ``tools/list``, ``tools/call``, ``ping`` and the
``notifications/initialized`` notification). Authentication reuses editube
personal access tokens: clients send ``Authorization: Bearer edt_…`` and
``get_current_user`` resolves the owner.

Only read-only, ownership-scoped tools are exposed.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from ...db.database import get_db
from app.db.models import Project, Video, VideoTranscription, User
from ...utils.security import get_current_user
from app.services.product_analytics import emit

router = APIRouter(prefix="/mcp", tags=["MCP"])

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "editube", "version": "1.0.0"}

TOOLS = [
    {
        "name": "list_projects",
        "description": "List the projects owned by the authenticated user.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_project_videos",
        "description": "List the videos in a project you own.",
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "integer", "description": "Project id"}},
            "required": ["project_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_video_transcript",
        "description": "Get the transcript text of a video you own.",
        "inputSchema": {
            "type": "object",
            "properties": {"video_id": {"type": "integer", "description": "Video id"}},
            "required": ["video_id"],
            "additionalProperties": False,
        },
    },
]


def _text_result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _tool_list_projects(db: Session, user: User) -> dict:
    projects = (
        db.query(Project)
        .filter(Project.creator_id == user.id)
        .order_by(Project.created_at.desc())
        .limit(100)
        .all()
    )
    if not projects:
        return _text_result("You have no projects yet.")
    lines = [
        f"- #{p.id} {p.name or 'Untitled'}" + (f" [{p.project_type}]" if p.project_type else "")
        for p in projects
    ]
    return _text_result("Your projects:\n" + "\n".join(lines))


def _tool_list_project_videos(db: Session, user: User, args: dict) -> dict:
    project_id = args.get("project_id")
    if not isinstance(project_id, int):
        return _text_result("`project_id` (integer) is required.", is_error=True)
    project = (
        db.query(Project)
        .filter(Project.id == project_id, Project.creator_id == user.id)
        .first()
    )
    if project is None:
        return _text_result(f"No project #{project_id} owned by you.", is_error=True)
    videos = db.query(Video).filter(Video.project_id == project_id).order_by(Video.id.desc()).all()
    if not videos:
        return _text_result(f"Project #{project_id} has no videos.")
    lines = [
        f"- #{v.id} {v.name or 'Untitled'} (status: {v.status}"
        + (f", {v.duration}s" if v.duration else "")
        + ")"
        for v in videos
    ]
    return _text_result(f"Videos in project #{project_id}:\n" + "\n".join(lines))


def _tool_get_video_transcript(db: Session, user: User, args: dict) -> dict:
    video_id = args.get("video_id")
    if not isinstance(video_id, int):
        return _text_result("`video_id` (integer) is required.", is_error=True)
    video = (
        db.query(Video)
        .join(Project, Video.project_id == Project.id)
        .filter(Video.id == video_id, Project.creator_id == user.id)
        .first()
    )
    if video is None:
        return _text_result(f"No video #{video_id} owned by you.", is_error=True)
    transcription = (
        db.query(VideoTranscription)
        .filter(VideoTranscription.video_id == video_id)
        .order_by(VideoTranscription.id.desc())
        .first()
    )
    if transcription is None:
        return _text_result(f"Video #{video_id} has no transcript.")
    segments = transcription.segments if isinstance(transcription.segments, list) else []
    parts = [
        str(seg.get("text", "")).strip()
        for seg in segments
        if isinstance(seg, dict) and seg.get("text")
    ]
    text = " ".join(parts).strip()
    if not text:
        return _text_result(
            f"Transcript for video #{video_id} is not ready (status: {transcription.status})."
        )
    return _text_result(text)


def _dispatch_tool(name: str, args: dict, db: Session, user: User) -> dict:
    if name == "list_projects":
        return _tool_list_projects(db, user)
    if name == "list_project_videos":
        return _tool_list_project_videos(db, user, args)
    if name == "get_video_transcript":
        return _tool_get_video_transcript(db, user, args)
    raise KeyError(name)


def _handle_message(msg: dict, db: Session, user: User):
    """Return a JSON-RPC response dict, or None for notifications."""
    method = msg.get("method")
    msg_id = msg.get("id")
    is_notification = "id" not in msg

    def result(payload):
        return {"jsonrpc": "2.0", "id": msg_id, "result": payload}

    def error(code, message):
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    if method == "initialize":
        return result(
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            }
        )
    if method == "ping":
        return result({})
    if method == "tools/list":
        return result({"tools": TOOLS})
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            payload = _dispatch_tool(name, args, db, user)
            if not payload.get("isError"):
                safe_tool_key = name if name in {tool["name"] for tool in TOOLS} else "unknown"
                emit(
                    db,
                    "mcp_connection_used",
                    user=user,
                    properties={
                        "feature_key": "mcp",
                        "tool_key": safe_tool_key,
                        "result": "success",
                    },
                )
                emit(
                    db,
                    "feature_completed",
                    user=user,
                    properties={
                        "feature_key": "mcp",
                        "tool_key": safe_tool_key,
                        "completion_type": "tool_operation",
                        "result": "success",
                    },
                )
                emit(
                    db,
                    "feature_result_used",
                    user=user,
                    properties={
                        "feature_key": "mcp",
                        "tool_key": safe_tool_key,
                        "result_type": "tool_result",
                        "result": "success",
                    },
                )
            return result(payload)
        except KeyError:
            return error(-32602, f"Unknown tool: {name}")
        except Exception as exc:  # surface tool failures without crashing the session
            return {"jsonrpc": "2.0", "id": msg_id, "result": _text_result(f"Error: {exc}", True)}

    if is_notification:
        # Notifications (e.g. notifications/initialized) require no response.
        return None
    return error(-32601, f"Method not found: {method}")


@router.post("")
async def mcp_endpoint(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            status_code=400,
        )

    # A JSON-RPC batch (list) or a single message.
    if isinstance(body, list):
        responses = [r for r in (_handle_message(m, db, current_user) for m in body) if r is not None]
        db.commit()
        if not responses:
            return Response(status_code=202)
        return JSONResponse(responses)

    response = _handle_message(body, db, current_user)
    db.commit()
    if response is None:
        return Response(status_code=202)
    return JSONResponse(response)
