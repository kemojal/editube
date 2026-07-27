"""Load, refresh and persist Google Drive OAuth credentials for a user.

Mirrors ``app/services/youtube_credentials.py`` deliberately rather than
generalising it — the YouTube flow is in production and this keeps the two
independent (different scopes, different table, different cardinality).

Scope is ``drive.file`` only, which grants access solely to files the user
picked through the Google Picker. See docs/google-drive-import-plan.md §1 for
why the broader ``drive.readonly`` (restricted, CASA-audited) is avoided.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

import google.auth.transport.requests
from google.oauth2.credentials import Credentials
from sqlalchemy.orm import Session

from app.db.models import UserGoogleDriveConnection
from app.utils.token_crypto import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)

DRIVE_SCOPES = (
    "https://www.googleapis.com/auth/drive.file",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
)


class DriveReauthRequired(RuntimeError):
    """The stored refresh token no longer works — the user must reconnect."""


def _client_config() -> tuple[str, str]:
    cid = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        raise RuntimeError("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set for Google Drive OAuth.")
    return cid, secret


def _naive_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def _scopes_to_text(scopes) -> str:
    if isinstance(scopes, str):
        return scopes
    if isinstance(scopes, (list, tuple)):
        return " ".join(scopes)
    return str(scopes)


def build_credentials_for_connection(row: UserGoogleDriveConnection) -> Credentials:
    client_id, client_secret = _client_config()
    refresh = decrypt_secret(row.refresh_token_encrypted)
    return Credentials(
        token=row.access_token,
        refresh_token=refresh,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=list(DRIVE_SCOPES),
        expiry=_naive_utc(row.access_expires_at),
    )


def persist_google_credentials(db: Session, row: UserGoogleDriveConnection, creds: Credentials) -> None:
    row.access_token = creds.token
    row.access_expires_at = _naive_utc(creds.expiry) or (datetime.utcnow() + timedelta(hours=1))
    db.add(row)
    db.commit()
    db.refresh(row)


def mark_revoked(db: Session, row: UserGoogleDriveConnection) -> None:
    """Flag a connection as needing re-consent so the UI can offer 'Reconnect'."""
    row.status = "revoked"
    db.add(row)
    db.commit()


def refresh_credentials_if_needed(db: Session, row: UserGoogleDriveConnection) -> Credentials:
    """Return usable credentials, refreshing + persisting when needed.

    Note ``Credentials.expired`` is ``False`` when ``expiry`` is ``None``, so a
    row that has never stored an access token would otherwise be handed back
    with ``token=None``. Force a refresh whenever there is no token.

    Raises ``DriveReauthRequired`` (and marks the row revoked) when Google
    rejects the refresh token — revoked access, password change, or the user
    removing our app from their Google account.
    """
    creds = build_credentials_for_connection(row)
    if creds.token and not creds.expired:
        return creds
    if not creds.refresh_token:
        mark_revoked(db, row)
        raise DriveReauthRequired("No refresh token stored for this Google Drive connection.")
    try:
        creds.refresh(google.auth.transport.requests.Request())
    except Exception as e:  # google.auth.exceptions.RefreshError and friends
        logger.warning("Google Drive token refresh failed for connection %s: %s", row.id, e)
        mark_revoked(db, row)
        raise DriveReauthRequired("Google Drive access expired. Reconnect the account.") from e
    persist_google_credentials(db, row, creds)
    return creds


def ensure_fresh_access_token(db: Session, row: UserGoogleDriveConnection) -> tuple[str, int]:
    """Access token + seconds until expiry, for handing to the browser Picker.

    Safe to expose to the same user's browser: ``drive.file`` can only ever
    reach files that user already picked with this app.
    """
    creds = refresh_credentials_if_needed(db, row)
    if not creds.token:
        # Would surface as Picker's setOAuthToken(null) with no useful message.
        raise DriveReauthRequired("Could not obtain a Google Drive access token.")
    expiry = _naive_utc(creds.expiry)
    if expiry is None:
        expires_in = 3600
    else:
        expires_in = int((expiry - datetime.utcnow()).total_seconds())
    return creds.token, max(0, expires_in)


def persist_tokens_from_exchange(
    db: Session,
    user_id: int,
    token_json: dict,
    *,
    google_sub: str,
    email: str | None = None,
    picture_url: str | None = None,
) -> UserGoogleDriveConnection:
    """Upsert the connection for ``(user_id, google_sub)`` from a code exchange.

    Reconnecting the *same* Google account updates the existing row instead of
    creating a duplicate; a *different* account adds a second connection.
    """
    refresh = token_json.get("refresh_token")
    if not refresh:
        raise ValueError(
            "Google did not return a refresh_token; try revoking app access and reconnect with prompt=consent."
        )

    access = token_json.get("access_token")
    expires_in = token_json.get("expires_in")
    expires_at = None
    if expires_in:
        expires_at = datetime.utcnow() + timedelta(seconds=int(expires_in))

    enc_refresh = encrypt_secret(refresh)
    scopes = _scopes_to_text(token_json.get("scope", ""))

    row = (
        db.query(UserGoogleDriveConnection)
        .filter(
            UserGoogleDriveConnection.user_id == user_id,
            UserGoogleDriveConnection.google_sub == google_sub,
        )
        .first()
    )
    if row:
        row.refresh_token_encrypted = enc_refresh
        row.access_token = access
        row.access_expires_at = expires_at
        row.scopes = scopes
        row.status = "active"
        if email:
            row.email = email
        if picture_url:
            row.picture_url = picture_url
    else:
        has_any = (
            db.query(UserGoogleDriveConnection)
            .filter(UserGoogleDriveConnection.user_id == user_id)
            .first()
            is not None
        )
        row = UserGoogleDriveConnection(
            user_id=user_id,
            google_sub=google_sub,
            email=email,
            picture_url=picture_url,
            refresh_token_encrypted=enc_refresh,
            access_token=access,
            access_expires_at=expires_at,
            scopes=scopes,
            status="active",
            # First account a user connects becomes their default.
            is_default=not has_any,
        )
        db.add(row)
    db.commit()
    db.refresh(row)
    return row
