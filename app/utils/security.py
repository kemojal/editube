import hashlib
import os
import time

from passlib.context import CryptContext
from datetime import datetime, timedelta
from jose import jwt, JWTError, ExpiredSignatureError
from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.db.database import get_db
from app.db.models import ApiToken, User, UserSettings, UserSession
from app.services.request_context import bind_user

# Personal access tokens are issued as ``edt_<40 hex>`` and stored only as a hash.
API_TOKEN_PREFIX = "edt_"


def hash_api_token(raw_token: str) -> str:
    """One-way hash used to store/look up personal access tokens."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def get_password_hash(password):
    return pwd_context.hash(password)

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

# Set JWT_SECRET_KEY in production (Dokploy / .env). Default is dev-only.
SECRET_KEY = os.getenv(
    "JWT_SECRET_KEY",
    "5f647556f4a1a426f08fad0bbb8bbab058aee16e6478e034c3a86855461b7e26",
)
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
REFRESH_TOKEN_EXPIRE_DAYS = 30

def create_access_token(data: dict, expires_minutes: int | None = None):
    """Mint a signed token.

    `expires_minutes` overrides the default lifetime. Short-lived tokens that
    are not sessions — the MFA challenge handed out between password and second
    factor — pass a few minutes here rather than living as long as a real
    access token.
    """
    to_encode = data.copy()
    minutes = ACCESS_TOKEN_EXPIRE_MINUTES if expires_minutes is None else expires_minutes
    expire = datetime.utcnow() + timedelta(minutes=minutes)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def create_user_session(db: Session, user_id: int) -> str:
    session_id = os.urandom(16).hex()
    session = UserSession(user_id=user_id, session_id=session_id)
    db.add(session)
    db.commit()
    return session_id


def previous_user_activity_gap_days(
    db: Session,
    user_id: int,
    *,
    now: datetime | None = None,
) -> int | None:
    """Whole days since the user's last authenticated session activity.

    Call this immediately before creating a new session. It deliberately uses
    server-side session history instead of browser storage, so a return from a
    different device can still qualify for the paid-user feedback trigger.
    """

    previous = (
        db.query(func.max(UserSession.last_activity_at))
        .filter(UserSession.user_id == user_id)
        .scalar()
    )
    if previous is None:
        return None
    elapsed = (now or datetime.utcnow()) - previous
    return max(0, int(elapsed.total_seconds() // 86_400))


def _session_timeout_minutes_for_user(db: Session, user_id: int) -> int:
    settings = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
    if not settings:
        return 30
    try:
        return int(settings.session_timeout)
    except (TypeError, ValueError):
        return 30


# In-process cache of recently validated sessions. A hit skips the session +
# settings round trips entirely — against a remote Postgres those two queries
# cost ~500ms on every request. Revocation and inactivity are enforced with at
# most TTL seconds of delay, negligible against minutes-scale session
# timeouts; explicit revocation clears its entry immediately (this process).
_SESSION_CACHE: dict[tuple[int, str], float] = {}
_SESSION_CACHE_TTL_SECONDS = 60.0
_SESSION_CACHE_MAX = 5000


def _validate_and_touch_session(db: Session, user_id: int, session_id: str) -> None:
    cache_key = (user_id, session_id)
    cached_at = _SESSION_CACHE.get(cache_key)
    if cached_at is not None and time.monotonic() - cached_at < _SESSION_CACHE_TTL_SECONDS:
        return
    session = (
        db.query(UserSession)
        .filter(UserSession.user_id == user_id, UserSession.session_id == session_id)
        .first()
    )
    if not session or session.revoked:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session is no longer active",
            headers={"WWW-Authenticate": "Bearer"},
        )

    now = datetime.utcnow()
    timeout_minutes = _session_timeout_minutes_for_user(db, user_id)
    inactivity_deadline = session.last_activity_at + timedelta(minutes=timeout_minutes)
    if now > inactivity_deadline:
        session.revoked = True
        session.revoked_at = now
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired due to inactivity",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # The timestamp only feeds a minutes-scale inactivity deadline, so a value
    # up to 60s stale is indistinguishable — and skipping the write keeps
    # frequent polling GETs from each paying a commit before their handler runs.
    if (now - session.last_activity_at) >= timedelta(seconds=60):
        session.last_activity_at = now
        db.commit()

    if len(_SESSION_CACHE) > _SESSION_CACHE_MAX:
        _SESSION_CACHE.clear()
    _SESSION_CACHE[cache_key] = time.monotonic()

def authenticate_api_token(db: Session, token: str) -> User:
    """Resolve a personal access token (``edt_…``) to its owning user."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid API token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    record = (
        db.query(ApiToken)
        .filter(ApiToken.token_hash == hash_api_token(token))
        .first()
    )
    if record is None:
        raise credentials_exception
    user = db.query(User).filter(User.id == record.user_id).first()
    if user is None or user.deleted_at is not None:
        raise credentials_exception
    first_use = record.last_used_at is None
    record.last_used_at = datetime.utcnow()
    db.commit()
    if first_use:
        # Import lazily to keep the security module free of analytics import
        # cycles during application startup. Provider failure is isolated by
        # the outbox helper and can never invalidate a successful API request.
        from app.services.product_analytics import emit_after_commit

        emit_after_commit(
            "feature_result_used",
            user_id=user.id,
            properties={
                "feature_key": "api_tokens",
                "result_action": "authenticated_api_request",
                "result": "success",
            },
            event_id=f"api-token:{record.id}:first-use",
        )
    return user


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    if token and token.startswith(API_TOKEN_PREFIX):
        user = authenticate_api_token(db, token)
    else:
        user = authenticate_access_token(db, token, touch_session=True)
    bind_user(
        user_id=user.id,
        plan=user.plan,
        subscription_status=user.subscription_status,
        user_role=user.role,
    )
    return user



def authenticate_user(db: Session, email: str, password: str):
    user = db.query(User).filter(User.email == email).first()
    if not user or user.deleted_at is not None:
        return None
    if not user.hashed_password:
        return None
    if not verify_password(password, user.hashed_password):
        return None
    return user


#refresh token

def create_refresh_token(data: dict):
    expire = datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    data.update({"exp": expire})
    return jwt.encode(data, SECRET_KEY, algorithm=ALGORITHM)

def verify_token(token: str, secret_key: str):
    try:
        payload = jwt.decode(token, secret_key, algorithms=[ALGORITHM])
        return payload
    except jwt.JWTError:
        return None

def verify_access_token(token: str):
    return verify_token(token, SECRET_KEY)

def verify_refresh_token(token: str):
    return verify_token(token, SECRET_KEY)


def decode_access_token_payload(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid access token")


def authenticate_access_token(db: Session, token: str, touch_session: bool = True) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("user_id")
        session_id = payload.get("sid")
        if user_id is None:
            raise credentials_exception
        if not session_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session missing. Please sign in again.",
                headers={"WWW-Authenticate": "Bearer"},
            )
    except ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except JWTError:
        raise credentials_exception

    user = db.query(User).filter(User.id == user_id).first()
    if user is None or user.deleted_at is not None:
        raise credentials_exception
    if touch_session:
        _validate_and_touch_session(db, user.id, session_id)
    return user


def validate_refresh_session(db: Session, payload: dict) -> int:
    user_id = payload.get("user_id")
    session_id = payload.get("sid")
    if user_id is None or not session_id:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    user = db.query(User).filter(User.id == int(user_id)).first()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    _validate_and_touch_session(db, user.id, session_id)
    return user.id


def revoke_user_session(db: Session, user_id: int, session_id: str) -> None:
    _SESSION_CACHE.pop((user_id, session_id), None)
    session = (
        db.query(UserSession)
        .filter(UserSession.user_id == user_id, UserSession.session_id == session_id)
        .first()
    )
    if not session or session.revoked:
        return
    session.revoked = True
    session.revoked_at = datetime.utcnow()
    db.commit()

def get_password_hash(password):
    return pwd_context.hash(password)

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)
