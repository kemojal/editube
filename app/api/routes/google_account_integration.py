import json
import os
from datetime import datetime, timedelta
from urllib import parse, request
from urllib.error import HTTPError, URLError

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from jose import ExpiredSignatureError, JWTError, jwt
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import User
from app.utils.security import ALGORITHM, SECRET_KEY, create_access_token, create_refresh_token, create_user_session

router = APIRouter(prefix="/users", tags=["Users"])


def _require_google_env() -> tuple[str, str]:
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise HTTPException(status_code=500, detail="Google OAuth is not configured")
    return client_id, client_secret


def _frontend_callback_url() -> str:
    return os.getenv("FRONTEND_GOOGLE_CALLBACK_URL", "http://localhost:3002/google/callback")


def _build_backend_callback_url(req: Request) -> str:
    explicit_callback = os.getenv("GOOGLE_REDIRECT_URI")
    if explicit_callback:
        return explicit_callback
    return str(req.url_for("google_oauth_callback"))


def _google_token_exchange(code: str, client_id: str, client_secret: str, redirect_uri: str) -> dict:
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
    with request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _google_userinfo(access_token: str) -> dict:
    req = request.Request(
        "https://openidconnect.googleapis.com/v1/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        method="GET",
    )
    with request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _safe_internal_next(raw: str | None) -> str | None:
    """Same-origin path only (open-redirect safe), aligned with frontend getSafeInternalRedirect."""
    if raw is None:
        return None
    s = raw.strip()
    if not s.startswith("/") or s.startswith("//") or "://" in s:
        return None
    return s


def _encode_google_oauth_state(next_path: str | None) -> str:
    payload: dict = {
        "purpose": "google_oauth_next",
        "exp": datetime.utcnow() + timedelta(minutes=15),
    }
    if next_path:
        payload["next"] = next_path
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def _decode_google_oauth_state(state: str | None) -> str | None:
    if not state or not state.strip():
        return None
    try:
        payload = jwt.decode(state.strip(), SECRET_KEY, algorithms=[ALGORITHM])
    except (JWTError, ExpiredSignatureError):
        return None
    if payload.get("purpose") != "google_oauth_next":
        return None
    return _safe_internal_next(payload.get("next"))


@router.get("/google/login")
def google_oauth_login(
    req: Request,
    next_path: str | None = Query(default=None, alias="next"),
):
    client_id, _ = _require_google_env()
    redirect_uri = _build_backend_callback_url(req)
    safe_next = _safe_internal_next(next_path)
    oauth_state = _encode_google_oauth_state(safe_next)
    params = parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "access_type": "offline",
            "prompt": "consent",
            "state": oauth_state,
        }
    )
    return RedirectResponse(url=f"https://accounts.google.com/o/oauth2/v2/auth?{params}", status_code=302)


@router.get("/google/callback", name="google_oauth_callback")
def google_oauth_callback(
    req: Request,
    code: str | None = None,
    error: str | None = None,
    state: str | None = None,
    db: Session = Depends(get_db),
):
    frontend_callback = _frontend_callback_url()
    return_next = _decode_google_oauth_state(state)

    if error:
        redirect_url = f"{frontend_callback}?error={parse.quote(error)}"
        return RedirectResponse(url=redirect_url, status_code=302)
    if not code:
        return RedirectResponse(url=f"{frontend_callback}?error=missing_code", status_code=302)

    client_id, client_secret = _require_google_env()
    redirect_uri = _build_backend_callback_url(req)

    try:
        token_data = _google_token_exchange(
            code=code,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
        )
        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail="Failed to fetch Google access token")

        google_user = _google_userinfo(access_token)
    except (HTTPError, URLError, TimeoutError, HTTPException):
        return RedirectResponse(url=f"{frontend_callback}?error=google_exchange_failed", status_code=302)

    email = google_user.get("email")
    google_sub = google_user.get("sub")
    name = google_user.get("name") or (email.split("@")[0] if email else "Google User")

    if not email or not google_sub:
        return RedirectResponse(url=f"{frontend_callback}?error=invalid_google_profile", status_code=302)

    user = db.query(User).filter(User.google_sub == google_sub).first()
    if not user:
        user = db.query(User).filter(User.email == email).first()
        if user:
            user.google_sub = google_sub
            user.auth_provider = user.auth_provider or "google"
        else:
            user = User(
                email=email,
                name=name,
                full_name=name,
                role="user",
                hashed_password=None,
                google_sub=google_sub,
                auth_provider="google",
            )
            db.add(user)
        db.commit()
        db.refresh(user)

    session_id = create_user_session(db, user.id)
    app_access_token = create_access_token(
        data={"user_id": user.id, "onboarding_completed": user.onboarding_completed, "sid": session_id}
    )
    app_refresh_token = create_refresh_token(data={"user_id": user.id, "sid": session_id})
    redirect_url = (
        f"{frontend_callback}?access_token={parse.quote(app_access_token)}"
        f"&refresh_token={parse.quote(app_refresh_token)}"
        f"&onboarding_completed={'true' if user.onboarding_completed else 'false'}"
    )
    if return_next:
        redirect_url += f"&next={parse.quote(return_next, safe='')}"
    return RedirectResponse(url=redirect_url, status_code=302)