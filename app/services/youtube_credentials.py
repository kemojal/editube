"""Load and refresh YouTube OAuth credentials for a user."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

import google.auth.transport.requests
from google.oauth2.credentials import Credentials
from sqlalchemy.orm import Session

from app.db.models import UserYoutubeConnection
from app.utils.token_crypto import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)

YOUTUBE_SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",
)


def _client_config() -> tuple[str, str]:
    cid = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        raise RuntimeError("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set for YouTube OAuth.")
    return cid, secret


def build_credentials_for_connection(row: UserYoutubeConnection) -> Credentials:
    client_id, client_secret = _client_config()
    refresh = decrypt_secret(row.refresh_token_encrypted)
    return Credentials(
        token=row.access_token,
        refresh_token=refresh,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=list(YOUTUBE_SCOPES),
        expiry=_naive_utc(row.access_expires_at),
    )


def _naive_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def persist_google_credentials(db: Session, row: UserYoutubeConnection, creds: Credentials) -> None:
    row.access_token = creds.token
    row.access_expires_at = _naive_utc(creds.expiry) or (datetime.utcnow() + timedelta(hours=1))
    db.add(row)
    db.commit()
    db.refresh(row)


def refresh_credentials_if_needed(db: Session, row: UserYoutubeConnection) -> Credentials:
    creds = build_credentials_for_connection(row)
    if creds.expired and creds.refresh_token:
        creds.refresh(google.auth.transport.requests.Request())
        persist_google_credentials(db, row, creds)
    return creds


def persist_tokens_from_exchange(
    db: Session,
    user_id: int,
    token_json: dict,
    *,
    existing: UserYoutubeConnection | None = None,
) -> UserYoutubeConnection:
    refresh = token_json.get("refresh_token")
    if not refresh:
        raise ValueError("Google did not return a refresh_token; try revoking app access and reconnect with prompt=consent.")

    access = token_json.get("access_token")
    expires_in = token_json.get("expires_in")
    expires_at = None
    if expires_in:
        expires_at = datetime.utcnow() + timedelta(seconds=int(expires_in))

    enc_refresh = encrypt_secret(refresh)
    scopes = token_json.get("scope", "")

    if existing:
        row = existing
        row.refresh_token_encrypted = enc_refresh
        row.access_token = access
        row.access_expires_at = expires_at
        row.scopes = scopes if isinstance(scopes, str) else " ".join(scopes) if isinstance(scopes, list) else str(scopes)
    else:
        row = UserYoutubeConnection(
            user_id=user_id,
            refresh_token_encrypted=enc_refresh,
            access_token=access,
            access_expires_at=expires_at,
            scopes=scopes if isinstance(scopes, str) else " ".join(scopes) if isinstance(scopes, list) else str(scopes),
        )
        db.add(row)
    db.commit()
    db.refresh(row)
    return row
