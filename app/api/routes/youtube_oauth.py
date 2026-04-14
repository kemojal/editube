"""YouTube Data API OAuth (separate from Google login)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from urllib import parse, request
from urllib.error import HTTPError, URLError

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import User, UserYoutubeConnection
from app.services.youtube_credentials import YOUTUBE_SCOPES, persist_tokens_from_exchange
from app.utils.security import ALGORITHM, SECRET_KEY, get_current_user

router = APIRouter(prefix="/users/google/youtube", tags=["YouTube"])


def _require_google_client() -> tuple[str, str]:
    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise HTTPException(status_code=500, detail="Google OAuth client is not configured")
    return client_id, client_secret


def _youtube_callback_url(req: Request) -> str:
    explicit = os.getenv("GOOGLE_YOUTUBE_REDIRECT_URI", "").strip()
    if explicit:
        return explicit
    return str(req.url_for("youtube_oauth_callback"))


def _frontend_return_url() -> str:
    return os.getenv(
        "FRONTEND_YOUTUBE_OAUTH_RETURN_URL",
        os.getenv("FRONTEND_BASE_URL", "http://localhost:3000").rstrip("/") + "/?youtube_connected=1",
    )


def _encode_state(user_id: int) -> str:
    expire = datetime.utcnow() + timedelta(minutes=15)
    return jwt.encode(
        {"sub": str(user_id), "typ": "youtube_oauth", "exp": expire},
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def _decode_state(state: str) -> int:
    try:
        payload = jwt.decode(state, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("typ") != "youtube_oauth":
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


def _fetch_channel_meta(access_token: str) -> tuple[str | None, str | None]:
    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.get(
                "https://www.googleapis.com/youtube/v3/channels",
                params={"part": "snippet", "mine": "true"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            r.raise_for_status()
            data = r.json()
            items = data.get("items") or []
            if not items:
                return None, None
            ch = items[0]
            cid = ch.get("id")
            title = (ch.get("snippet") or {}).get("title")
            return cid, title
    except Exception:
        return None, None


@router.post("/authorize-url")
def youtube_authorize_url(req: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    client_id, _ = _require_google_client()
    redirect_uri = _youtube_callback_url(req)
    state = _encode_state(current_user.id)
    params = parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(YOUTUBE_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    url = f"https://accounts.google.com/o/oauth2/v2/auth?{params}"
    return {"authorization_url": url}


@router.get("/callback", name="youtube_oauth_callback")
def youtube_oauth_callback(
    req: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    ret = _frontend_return_url()
    if error:
        return RedirectResponse(url=f"{ret}&youtube_error={parse.quote(error)}", status_code=302)
    if not code or not state:
        return RedirectResponse(url=f"{ret}&youtube_error=missing_params", status_code=302)

    user_id = _decode_state(state)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return RedirectResponse(url=f"{ret}&youtube_error=unknown_user", status_code=302)

    client_id, client_secret = _require_google_client()
    redirect_uri = _youtube_callback_url(req)

    try:
        token_data = _token_exchange(code, client_id, client_secret, redirect_uri)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        return RedirectResponse(url=f"{ret}&youtube_error=token_exchange_failed", status_code=302)

    access = token_data.get("access_token")
    if not access:
        return RedirectResponse(url=f"{ret}&youtube_error=no_access_token", status_code=302)

    try:
        existing = db.query(UserYoutubeConnection).filter(UserYoutubeConnection.user_id == user.id).first()
        row = persist_tokens_from_exchange(db, user.id, token_data, existing=existing)
        ch_id, ch_title = _fetch_channel_meta(access)
        if ch_id or ch_title:
            row.channel_id = ch_id or row.channel_id
            row.channel_title = ch_title or row.channel_title
            db.add(row)
            db.commit()
    except ValueError as e:
        return RedirectResponse(url=f"{ret}&youtube_error={parse.quote(str(e)[:200])}", status_code=302)
    except RuntimeError:
        return RedirectResponse(url=f"{ret}&youtube_error=encryption_not_configured", status_code=302)

    return RedirectResponse(url=f"{ret}&youtube_success=1", status_code=302)


@router.get("/status")
def youtube_status(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = db.query(UserYoutubeConnection).filter(UserYoutubeConnection.user_id == current_user.id).first()
    if not row:
        return {"connected": False, "channel_id": None, "channel_title": None}
    return {
        "connected": True,
        "channel_id": row.channel_id,
        "channel_title": row.channel_title,
    }


@router.delete("/disconnect")
def youtube_disconnect(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = db.query(UserYoutubeConnection).filter(UserYoutubeConnection.user_id == current_user.id).first()
    if row:
        db.delete(row)
        db.commit()
    return {"ok": True}
