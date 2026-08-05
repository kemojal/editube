"""Zoom OAuth connection + cloud-recording listing.

Mirrors the Google Drive connection flow: a popup completes OAuth and
``postMessage``s the result back to the app. Tokens are stored with the refresh
token Fernet-encrypted; the short-lived access token is refreshed on demand.

Requires env: ``ZOOM_CLIENT_ID``, ``ZOOM_CLIENT_SECRET`` and (recommended)
``ZOOM_REDIRECT_URI`` byte-matching a redirect URL registered in the Zoom app.
"""

import base64
import json
import logging
import os
from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import User, UserZoomConnection
from app.utils.security import ALGORITHM, SECRET_KEY, get_current_user
from app.utils.token_crypto import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users/zoom", tags=["Zoom"])

ZOOM_AUTHORIZE = "https://zoom.us/oauth/authorize"
ZOOM_TOKEN = "https://zoom.us/oauth/token"
ZOOM_REVOKE = "https://zoom.us/oauth/revoke"
ZOOM_API = "https://api.zoom.us/v2"


def _require_zoom_client() -> tuple[str, str]:
    client_id = os.getenv("ZOOM_CLIENT_ID", "").strip()
    client_secret = os.getenv("ZOOM_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise HTTPException(status_code=500, detail="Zoom is not configured")
    return client_id, client_secret


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _frontend_origin() -> str:
    return os.getenv("FRONTEND_BASE_URL", "http://localhost:3000").rstrip("/")


def _zoom_callback_url(req: Request) -> str:
    """redirect_uri sent to Zoom. Must byte-match a URI registered in the app."""
    explicit = os.getenv("ZOOM_REDIRECT_URI", "").strip()
    if explicit:
        return explicit
    derived = str(req.url_for("zoom_oauth_callback"))
    logger.warning(
        "ZOOM_REDIRECT_URI is not set; deriving %s from the request. "
        "Register that exact URI in the Zoom app, or set the env var.",
        derived,
    )
    return derived


def _encode_state(user_id: int) -> str:
    expire = datetime.utcnow() + timedelta(minutes=15)
    return jwt.encode(
        {"sub": str(user_id), "typ": "zoom_oauth", "exp": expire}, SECRET_KEY, algorithm=ALGORITHM
    )


def _decode_state(state: str) -> int:
    try:
        payload = jwt.decode(state, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("typ") != "zoom_oauth":
            raise ValueError("wrong typ")
        return int(payload["sub"])
    except (JWTError, ValueError, KeyError, TypeError) as e:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state") from e


def _token_request(data: dict) -> dict:
    client_id, client_secret = _require_zoom_client()
    with httpx.Client(timeout=30.0) as client:
        r = client.post(
            ZOOM_TOKEN,
            data=data,
            headers={
                "Authorization": _basic_auth_header(client_id, client_secret),
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        r.raise_for_status()
        return r.json()


def _fetch_zoom_user(access_token: str) -> dict:
    with httpx.Client(timeout=20.0) as client:
        r = client.get(f"{ZOOM_API}/users/me", headers={"Authorization": f"Bearer {access_token}"})
        r.raise_for_status()
        return r.json() or {}


def _persist(db: Session, user_id: int, token_data: dict, info: dict) -> UserZoomConnection:
    refresh = token_data.get("refresh_token")
    access = token_data.get("access_token")
    if not refresh or not access:
        raise ValueError("missing_tokens")
    expires_in = int(token_data.get("expires_in") or 3600)

    row = db.query(UserZoomConnection).filter(UserZoomConnection.user_id == user_id).first()
    if row is None:
        row = UserZoomConnection(user_id=user_id, zoom_user_id=str(info.get("id") or ""))
        db.add(row)
    row.zoom_user_id = str(info.get("id") or row.zoom_user_id or "")
    row.email = info.get("email")
    first = (info.get("first_name") or "").strip()
    last = (info.get("last_name") or "").strip()
    row.display_name = (f"{first} {last}".strip() or info.get("display_name") or None)
    row.refresh_token_encrypted = encrypt_secret(refresh)
    row.access_token = access
    row.access_expires_at = datetime.utcnow() + timedelta(seconds=expires_in - 60)
    row.status = "active"
    db.commit()
    db.refresh(row)
    return row


def _valid_access_token(db: Session, row: UserZoomConnection) -> str:
    """Return a live access token, refreshing via the refresh token if needed."""
    now = datetime.utcnow()
    if row.access_token and row.access_expires_at and row.access_expires_at > now:
        return row.access_token
    refresh = decrypt_secret(row.refresh_token_encrypted)
    token_data = _token_request({"grant_type": "refresh_token", "refresh_token": refresh})
    row.access_token = token_data.get("access_token")
    # Zoom rotates the refresh token on each refresh.
    if token_data.get("refresh_token"):
        row.refresh_token_encrypted = encrypt_secret(token_data["refresh_token"])
    row.access_expires_at = now + timedelta(seconds=int(token_data.get("expires_in") or 3600) - 60)
    db.commit()
    return row.access_token


def _js(value) -> str:
    return json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e")


def _popup_response(payload: dict) -> HTMLResponse:
    origin = _frontend_origin()
    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Zoom</title>
<style>
  body {{ margin:0; display:flex; align-items:center; justify-content:center; height:100vh;
    font:13px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:#71717a; background:#fff; }}
  @media (prefers-color-scheme: dark) {{ body {{ background:#09090b; color:#a1a1aa; }} }}
</style></head>
<body>
<p>Finishing up&hellip; you can close this window.</p>
<script>
  (function () {{
    var payload = {_js(payload)};
    var target = {_js(origin)};
    try {{
      if (window.opener && !window.opener.closed) {{
        window.opener.postMessage(Object.assign({{ source: "editube:zoom" }}, payload), target);
        window.close();
        return;
      }}
    }} catch (e) {{ /* cross-origin opener — fall through */ }}
    var qs = payload.ok ? "zoom_connected=1" : "zoom_error=" + encodeURIComponent(payload.error || "failed");
    window.location.replace(target + "/dashboard?" + qs);
  }})();
</script>
</body></html>"""
    return HTMLResponse(content=body)


def _status_payload(row: UserZoomConnection | None) -> dict:
    if row is None or row.status != "active":
        return {"connected": False, "email": None, "display_name": None}
    return {"connected": True, "email": row.email, "display_name": row.display_name}


@router.post("/authorize-url")
def zoom_authorize_url(req: Request, current_user: User = Depends(get_current_user)):
    client_id, _ = _require_zoom_client()
    from urllib.parse import urlencode

    params = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": _zoom_callback_url(req),
            "state": _encode_state(current_user.id),
        }
    )
    return {"authorization_url": f"{ZOOM_AUTHORIZE}?{params}"}


@router.get("/callback", name="zoom_oauth_callback")
def zoom_oauth_callback(
    req: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    if error:
        return _popup_response({"ok": False, "error": error})
    if not code or not state:
        return _popup_response({"ok": False, "error": "missing_params"})
    try:
        user_id = _decode_state(state)
    except HTTPException:
        return _popup_response({"ok": False, "error": "invalid_state"})
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return _popup_response({"ok": False, "error": "unknown_user"})

    try:
        token_data = _token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _zoom_callback_url(req),
            }
        )
    except httpx.HTTPError:
        return _popup_response({"ok": False, "error": "token_exchange_failed"})

    access = token_data.get("access_token")
    if not access:
        return _popup_response({"ok": False, "error": "no_access_token"})
    try:
        info = _fetch_zoom_user(access)
    except httpx.HTTPError:
        info = {}
    try:
        row = _persist(db, user.id, token_data, info)
    except ValueError as e:
        return _popup_response({"ok": False, "error": str(e)[:200]})
    except RuntimeError:
        return _popup_response({"ok": False, "error": "encryption_not_configured"})

    return _popup_response({"ok": True, "connection": _status_payload(row)})


@router.get("/status")
def zoom_status(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = db.query(UserZoomConnection).filter(UserZoomConnection.user_id == current_user.id).first()
    return _status_payload(row)


@router.delete("/disconnect")
def zoom_disconnect(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = db.query(UserZoomConnection).filter(UserZoomConnection.user_id == current_user.id).first()
    if row is None:
        return {"ok": True}
    # Best-effort revoke at Zoom so the grant disappears from the user's account.
    try:
        refresh = decrypt_secret(row.refresh_token_encrypted)
        client_id, client_secret = _require_zoom_client()
        with httpx.Client(timeout=10.0) as client:
            client.post(
                ZOOM_REVOKE,
                data={"token": refresh},
                headers={
                    "Authorization": _basic_auth_header(client_id, client_secret),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
    except Exception as e:
        logger.info("Zoom token revoke skipped: %s", e)
    db.delete(row)
    db.commit()
    return {"ok": True}


@router.get("/recordings")
def zoom_recordings(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """List the connected account's Zoom cloud recordings (most recent first)."""
    row = db.query(UserZoomConnection).filter(UserZoomConnection.user_id == current_user.id).first()
    if row is None or row.status != "active":
        raise HTTPException(status_code=400, detail="Zoom is not connected")
    try:
        token = _valid_access_token(db, row)
        with httpx.Client(timeout=30.0) as client:
            r = client.get(
                f"{ZOOM_API}/users/me/recordings",
                params={"page_size": 30},
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()
            data = r.json() or {}
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Zoom API error: {e}")

    recordings = []
    for meeting in data.get("meetings", []) or []:
        files = [
            {
                "id": f.get("id"),
                "file_type": f.get("file_type"),
                "file_size": f.get("file_size"),
                "recording_type": f.get("recording_type"),
                "download_url": f.get("download_url"),
                "play_url": f.get("play_url"),
            }
            for f in (meeting.get("recording_files") or [])
            if f.get("file_type") in {"MP4", "M4A"}
        ]
        if not files:
            continue
        recordings.append(
            {
                "uuid": meeting.get("uuid"),
                "meeting_id": meeting.get("id"),
                "topic": meeting.get("topic"),
                "start_time": meeting.get("start_time"),
                "duration": meeting.get("duration"),
                "total_size": meeting.get("total_size"),
                "files": files,
            }
        )
    return {"recordings": recordings}
