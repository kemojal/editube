"""Google Drive OAuth + file import (create-project wizard source).

Separate from both Google *login* (``google_account_integration.py``, which
discards Google's tokens) and the YouTube Data API connection
(``youtube_oauth.py``, one account per user). See
docs/google-drive-import-plan.md.

The OAuth callback here returns a tiny HTML page that ``postMessage``s its
result to ``window.opener`` instead of redirecting the top-level window: the
connect flow is launched from inside the New Project modal, and a redirect
would destroy all wizard state.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from urllib import parse, request
from urllib.error import HTTPError, URLError

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import DriveImport, User, UserGoogleDriveConnection
from app.jobs.queue import enqueue_drive_import_job
from app.services.google_drive_credentials import (
    DRIVE_SCOPES,
    DriveReauthRequired,
    ensure_fresh_access_token,
    persist_tokens_from_exchange,
    refresh_credentials_if_needed,
)
from app.services.google_drive_files import (
    DriveFileError,
    build_drive_service,
    fetch_file_metadata,
)
from app.services.storage_policy import assert_storage_upload_allowed
from app.services.product_analytics import emit, emit_after_commit
from app.services.job_analytics import record_job_canceled
from app.services.workspace_bootstrap import ensure_personal_workspace
from app.utils.security import ALGORITHM, SECRET_KEY, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users/google/drive", tags=["Google Drive"])

_TERMINAL_STATUSES = ("completed", "failed", "canceled")


# --- config helpers -------------------------------------------------------


def _require_google_client() -> tuple[str, str]:
    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise HTTPException(status_code=500, detail="Google OAuth client is not configured")
    return client_id, client_secret


def _drive_callback_url(req: Request) -> str:
    """The redirect_uri sent to Google. MUST byte-match a URI registered in the
    Google Cloud console, or Google returns ``Error 400: redirect_uri_mismatch``.

    Prefer the explicit env var: the ``url_for`` fallback derives the host from
    however the browser happened to reach this API, so the same deployment can
    emit ``localhost``, ``127.0.0.1`` or a tunnel hostname on different requests
    — each of which is a *different* URI as far as Google is concerned.
    """
    explicit = os.getenv("GOOGLE_DRIVE_REDIRECT_URI", "").strip()
    if explicit:
        return explicit
    derived = str(req.url_for("google_drive_oauth_callback"))
    logger.warning(
        "GOOGLE_DRIVE_REDIRECT_URI is not set; deriving %s from the request. "
        "Register that exact URI in the Google Cloud console, or set the env var.",
        derived,
    )
    return derived


def _frontend_origin() -> str:
    return os.getenv("FRONTEND_BASE_URL", "http://localhost:3000").rstrip("/")


def _encode_state(user_id: int) -> str:
    expire = datetime.utcnow() + timedelta(minutes=15)
    return jwt.encode(
        {"sub": str(user_id), "typ": "drive_oauth", "exp": expire},
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def _decode_state(state: str) -> int:
    try:
        payload = jwt.decode(state, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("typ") != "drive_oauth":
            raise ValueError("wrong typ")
        return int(payload["sub"])
    except (JWTError, ValueError, KeyError, TypeError) as e:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state") from e


def _token_exchange(code: str, client_id: str, client_secret: str, redirect_uri: str) -> dict:
    payload = parse.urlencode(
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")
    req = request.Request(
        "https://oauth2.googleapis.com/token",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _fetch_userinfo(access_token: str) -> dict:
    """sub/email/picture for the connected account. Best-effort except `sub`."""
    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.get(
                "https://openidconnect.googleapis.com/v1/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            r.raise_for_status()
            return r.json() or {}
    except Exception as e:
        logger.warning("Drive userinfo fetch failed: %s", e)
        return {}


def _js(value) -> str:
    """JSON for embedding in an inline <script>.

    ``json.dumps`` does not escape ``<``/``>``, so a payload carrying
    ``</script>`` — and ``error`` comes straight off the unauthenticated
    callback query string — would break out of the script element and execute
    on the API origin. Escape the three characters that matter.
    """
    return (
        json.dumps(value)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _popup_response(payload: dict) -> HTMLResponse:
    """HTML that posts ``payload`` to the opener and closes itself.

    Falls back to a redirect when there is no opener (user pasted the URL, or
    the browser blocked the popup and we used the same-tab path).
    """
    origin = _frontend_origin()
    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Google Drive</title>
<style>
  body {{ margin:0; display:flex; align-items:center; justify-content:center; height:100vh;
    font:13px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:#71717a;
    background:#fff; }}
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
        window.opener.postMessage(Object.assign({{ source: "editube:google-drive" }}, payload), target);
        window.close();
        return;
      }}
    }} catch (e) {{ /* cross-origin opener access — fall through to redirect */ }}
    var qs = payload.ok ? "drive_connected=1" : "drive_error=" + encodeURIComponent(payload.error || "failed");
    window.location.replace(target + "/dashboard?" + qs);
  }})();
</script>
</body></html>"""
    return HTMLResponse(content=body)


def _connection_payload(row: UserGoogleDriveConnection) -> dict:
    return {
        "id": row.id,
        "email": row.email,
        "picture_url": row.picture_url,
        "is_default": bool(row.is_default),
        "status": row.status,
    }


def _import_payload(row: DriveImport) -> dict:
    return {
        "id": row.id,
        "status": row.status,
        "drive_file_id": row.drive_file_id,
        "file_name": row.file_name,
        "mime_type": row.mime_type,
        "total_bytes": int(row.total_bytes or 0),
        "bytes_transferred": int(row.bytes_transferred or 0),
        "progress_percent": int(row.progress_percent or 0),
        "duration_seconds": row.duration_seconds,
        "thumbnail_url": row.thumbnail_url,
        "file_path": row.file_path,
        "error_code": row.error_code,
        "error_message": row.error_message,
    }


def _get_connection(db: Session, user_id: int, connection_id: int) -> UserGoogleDriveConnection:
    row = (
        db.query(UserGoogleDriveConnection)
        .filter(
            UserGoogleDriveConnection.id == connection_id,
            UserGoogleDriveConnection.user_id == user_id,
        )
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Google Drive connection not found")
    return row


def _record_oauth_failure(db: Session, state: str | None, error_code: str) -> None:
    if not state:
        return
    try:
        user_id = _decode_state(state)
    except HTTPException:
        return
    emit(
        db,
        "integration_connect_failed",
        user_id=user_id,
        properties={
            "provider": "google_drive",
            "feature_key": "google_drive",
            "error_code": error_code,
            "result": "failure",
        },
    )
    emit(
        db,
        "feature_failed",
        user_id=user_id,
        properties={
            "feature_key": "google_drive",
            "error_code": error_code,
            "failure_class": "oauth",
            "result": "failure",
        },
    )
    db.commit()


# --- OAuth ----------------------------------------------------------------


@router.post("/authorize-url")
def drive_authorize_url(
    req: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    client_id, _ = _require_google_client()
    params = parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": _drive_callback_url(req),
            "response_type": "code",
            "scope": " ".join(DRIVE_SCOPES),
            "access_type": "offline",
            # `select_account` is what actually offers the account chooser, so
            # "Add another account" can reach a second Google account instead of
            # silently re-consenting the one they're already signed into.
            "prompt": "select_account consent",
            "state": _encode_state(current_user.id),
        }
    )
    # FastAPI always injects a Session. The guard preserves the route helper's
    # long-standing direct-call use in OAuth URL unit tests.
    if isinstance(db, Session):
        emit(
            db,
            "integration_connect_started",
            user=current_user,
            properties={"provider": "google_drive", "feature_key": "google_drive"},
        )
        db.commit()
    return {"authorization_url": f"https://accounts.google.com/o/oauth2/v2/auth?{params}"}


@router.get("/callback", name="google_drive_oauth_callback")
def google_drive_oauth_callback(
    req: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    if error:
        _record_oauth_failure(db, state, "access_denied" if error == "access_denied" else "oauth_error")
        # access_denied is the user clicking "Cancel" — not an error worth shouting about.
        return _popup_response({"ok": False, "error": error})
    if not code or not state:
        _record_oauth_failure(db, state, "missing_params")
        return _popup_response({"ok": False, "error": "missing_params"})

    try:
        user_id = _decode_state(state)
    except HTTPException:
        return _popup_response({"ok": False, "error": "invalid_state"})

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return _popup_response({"ok": False, "error": "unknown_user"})

    client_id, client_secret = _require_google_client()
    try:
        token_data = _token_exchange(code, client_id, client_secret, _drive_callback_url(req))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        _record_oauth_failure(db, state, "token_exchange_failed")
        return _popup_response({"ok": False, "error": "token_exchange_failed"})

    access = token_data.get("access_token")
    if not access:
        _record_oauth_failure(db, state, "no_access_token")
        return _popup_response({"ok": False, "error": "no_access_token"})

    info = _fetch_userinfo(access)
    google_sub = info.get("sub")
    if not google_sub:
        _record_oauth_failure(db, state, "no_account_identity")
        return _popup_response({"ok": False, "error": "no_account_identity"})

    try:
        row = persist_tokens_from_exchange(
            db,
            user.id,
            token_data,
            google_sub=str(google_sub),
            email=info.get("email"),
            picture_url=info.get("picture"),
        )
    except ValueError as e:
        _record_oauth_failure(db, state, "credential_rejected")
        return _popup_response({"ok": False, "error": str(e)[:200]})
    except RuntimeError:
        _record_oauth_failure(db, state, "encryption_not_configured")
        return _popup_response({"ok": False, "error": "encryption_not_configured"})

    emit_after_commit(
        "integration_connected",
        user_id=user.id,
        properties={"provider": "google_drive", "feature_key": "google_drive", "result": "success"},
    )
    emit_after_commit(
        "feature_completed",
        user_id=user.id,
        properties={"feature_key": "google_drive", "completion_type": "oauth_connected", "result": "success"},
    )
    return _popup_response({"ok": True, "connection": _connection_payload(row)})


@router.get("/status")
def drive_status(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    rows = (
        db.query(UserGoogleDriveConnection)
        .filter(UserGoogleDriveConnection.user_id == current_user.id)
        .order_by(UserGoogleDriveConnection.is_default.desc(), UserGoogleDriveConnection.id.asc())
        .all()
    )
    return {"connections": [_connection_payload(r) for r in rows]}


@router.delete("/connections/{connection_id}")
def drive_disconnect(
    connection_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = _get_connection(db, current_user.id, connection_id)
    was_default = bool(row.is_default)

    # Best-effort revoke at Google so the grant disappears from the user's
    # account page too, not just from our DB.
    try:
        from app.utils.token_crypto import decrypt_secret

        refresh = decrypt_secret(row.refresh_token_encrypted)
        if refresh:
            with httpx.Client(timeout=10.0) as client:
                client.post(
                    "https://oauth2.googleapis.com/revoke",
                    data={"token": refresh},
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
    except Exception as e:
        logger.info("Google token revoke skipped for connection %s: %s", connection_id, e)

    db.delete(row)
    emit(
        db,
        "integration_disconnected",
        user=current_user,
        properties={"provider": "google_drive", "feature_key": "google_drive"},
    )
    db.commit()

    if was_default:
        promote = (
            db.query(UserGoogleDriveConnection)
            .filter(UserGoogleDriveConnection.user_id == current_user.id)
            .order_by(UserGoogleDriveConnection.id.asc())
            .first()
        )
        if promote:
            promote.is_default = True
            db.add(promote)
            db.commit()

    return {"ok": True}


# --- Picker ---------------------------------------------------------------


@router.get("/picker-token")
def drive_picker_token(
    connection_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Short-lived access token + Picker config.

    Serving ``app_id``/``developer_key`` from here (rather than
    ``NEXT_PUBLIC_*``) keeps the frontend env clean and makes key rotation a
    backend-only change.
    """
    if connection_id is not None:
        row = _get_connection(db, current_user.id, connection_id)
    else:
        row = (
            db.query(UserGoogleDriveConnection)
            .filter(
                UserGoogleDriveConnection.user_id == current_user.id,
                UserGoogleDriveConnection.status == "active",
            )
            .order_by(UserGoogleDriveConnection.is_default.desc(), UserGoogleDriveConnection.id.asc())
            .first()
        )
        if not row:
            raise HTTPException(status_code=404, detail="No Google Drive account connected")

    developer_key = os.getenv("GOOGLE_PICKER_API_KEY", "").strip()
    app_id = os.getenv("GOOGLE_PICKER_APP_ID", "").strip()
    if not developer_key or not app_id:
        raise HTTPException(
            status_code=500,
            detail="Google Picker is not configured (GOOGLE_PICKER_API_KEY / GOOGLE_PICKER_APP_ID).",
        )

    try:
        access_token, expires_in = ensure_fresh_access_token(db, row)
    except DriveReauthRequired as e:
        raise HTTPException(status_code=409, detail={"code": "reauth_required", "message": str(e)}) from e

    return {
        "connection_id": row.id,
        "access_token": access_token,
        "expires_in": expires_in,
        "app_id": app_id,
        "developer_key": developer_key,
    }


# --- Resolve + import -----------------------------------------------------


class DriveResolveRequest(BaseModel):
    connection_id: int
    file_ids: list[str]


@router.post("/resolve")
def drive_resolve(
    body: DriveResolveRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Validate picked files and return metadata, before any bytes move.

    Every gate runs here so the user is never told "no" halfway through a
    multi-gigabyte transfer.
    """
    if not body.file_ids:
        raise HTTPException(status_code=422, detail="No files selected")

    row = _get_connection(db, current_user.id, body.connection_id)
    try:
        creds = refresh_credentials_if_needed(db, row)
    except DriveReauthRequired as e:
        raise HTTPException(status_code=409, detail={"code": "reauth_required", "message": str(e)}) from e

    service = build_drive_service(creds)
    workspace = ensure_personal_workspace(db, current_user)

    files: list[dict] = []
    for file_id in body.file_ids[:10]:
        try:
            meta = fetch_file_metadata(service, file_id)
        except DriveFileError as e:
            files.append({"file_id": file_id, "ok": False, "error_code": e.code, "error_message": e.message})
            continue

        # Storage cap gate — surface the existing 402 copy up front.
        try:
            assert_storage_upload_allowed(
                db,
                user=current_user,
                workspace_id=workspace.id,
                incoming_bytes=meta.size_bytes,
            )
        except ValueError:
            files.append(
                {
                    "file_id": file_id,
                    "ok": False,
                    "error_code": "storage_cap_exceeded",
                    "error_message": "Storage cap reached and grace period ended.",
                }
            )
            continue

        files.append({"ok": True, "warnings": meta.warnings, **meta.to_payload()})

    return {"connection_id": row.id, "files": files}


class DriveImportRequest(BaseModel):
    connection_id: int
    file_id: str


@router.post("/imports")
def create_drive_import(
    body: DriveImportRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = _get_connection(db, current_user.id, body.connection_id)
    try:
        creds = refresh_credentials_if_needed(db, row)
    except DriveReauthRequired as e:
        raise HTTPException(status_code=409, detail={"code": "reauth_required", "message": str(e)}) from e

    service = build_drive_service(creds)
    try:
        meta = fetch_file_metadata(service, body.file_id)
    except DriveFileError as e:
        raise HTTPException(status_code=422, detail={"code": e.code, "message": e.message}) from e

    workspace = ensure_personal_workspace(db, current_user)
    try:
        assert_storage_upload_allowed(
            db, user=current_user, workspace_id=workspace.id, incoming_bytes=meta.size_bytes
        )
    except ValueError as e:
        raise HTTPException(
            status_code=402, detail="Storage cap reached and grace period ended."
        ) from e

    record = DriveImport(
        user_id=current_user.id,
        connection_id=row.id,
        drive_file_id=meta.file_id,
        file_name=meta.name,
        mime_type=meta.mime_type,
        total_bytes=meta.size_bytes,
        bytes_transferred=0,
        progress_percent=0,
        duration_seconds=meta.duration_seconds or None,
        thumbnail_url=meta.thumbnail_url,
        status="queued",
    )
    db.add(record)
    db.flush()
    emit(
        db,
        "integration_import_started",
        user=current_user,
        workspace_id=workspace.id,
        properties={
            "provider": "google_drive",
            "feature_key": "google_drive",
            "import_id": record.id,
            "file_count": 1,
        },
    )
    emit(
        db,
        "feature_started",
        user=current_user,
        workspace_id=workspace.id,
        properties={"feature_key": "google_drive", "entry_point": "file_import"},
    )
    db.commit()
    db.refresh(record)

    job_id = enqueue_drive_import_job(record.id)
    if not job_id:
        record.status = "failed"
        record.error_code = "queue_unavailable"
        record.error_message = (
            "Import could not be queued. Check REDIS_URL and that an RQ worker is running."
        )
        db.add(record)
        emit(
            db,
            "integration_import_failed",
            user=current_user,
            workspace_id=workspace.id,
            properties={
                "provider": "google_drive",
                "feature_key": "google_drive",
                "import_id": record.id,
                "error_code": "queue_unavailable",
                "result": "failure",
            },
        )
        emit(
            db,
            "feature_failed",
            user=current_user,
            workspace_id=workspace.id,
            properties={
                "feature_key": "google_drive",
                "error_code": "queue_unavailable",
                "failure_class": "queue",
                "result": "failure",
            },
        )
        db.commit()
        db.refresh(record)

    return _import_payload(record)


@router.get("/imports/{import_id}")
def get_drive_import(
    import_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = (
        db.query(DriveImport)
        .filter(DriveImport.id == import_id, DriveImport.user_id == current_user.id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Import not found")
    return _import_payload(row)


@router.post("/imports/{import_id}/cancel")
def cancel_drive_import(
    import_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Mark an import canceled so the worker stops on its next chunk.

    Called when the user removes the picked file or discards the wizard — no
    reason to keep pulling gigabytes nobody wants.
    """
    row = (
        db.query(DriveImport)
        .filter(DriveImport.id == import_id, DriveImport.user_id == current_user.id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Import not found")
    if row.status not in _TERMINAL_STATUSES:
        row.status = "canceled"
        db.add(row)
        emit(
            db,
            "integration_import_canceled",
            user=current_user,
            properties={
                "provider": "google_drive",
                "feature_key": "google_drive",
                "import_id": row.id,
            },
        )
        record_job_canceled(
            db,
            job_kind="google_drive_import",
            job_id=row.id,
            feature_key="google_drive",
            user=current_user,
        )
        db.commit()
        db.refresh(row)
    return _import_payload(row)
