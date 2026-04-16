from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from urllib import parse, request
from urllib.error import URLError

from jose import JWTError, jwt

from app.db.models import WorkspaceSSOProvider
from app.utils.security import ALGORITHM, SECRET_KEY


def build_oidc_authorize_url(provider: WorkspaceSSOProvider, *, redirect_uri: str) -> tuple[str, str]:
    state = secrets.token_urlsafe(18)
    endpoint = provider.authorization_endpoint or f"{provider.issuer.rstrip('/')}/v1/authorize"
    params = parse.urlencode(
        {
            "client_id": provider.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": provider.scope or "openid profile email",
            "state": state,
        }
    )
    return f"{endpoint}?{params}", state


def build_signed_sso_state(*, provider_id: int, return_path: str | None = None, ttl_minutes: int = 10) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "provider_id": int(provider_id),
        "return_path": return_path or "/",
        "nonce": secrets.token_urlsafe(12),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=ttl_minutes)).timestamp()),
        "purpose": "sso_state",
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def verify_signed_sso_state(raw_state: str) -> dict:
    try:
        payload = jwt.decode(raw_state, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError as exc:
        raise ValueError("Invalid SSO state") from exc
    if payload.get("purpose") != "sso_state":
        raise ValueError("Invalid SSO state purpose")
    if "provider_id" not in payload:
        raise ValueError("Missing provider id in SSO state")
    return payload


def discover_oidc_metadata(issuer: str) -> dict:
    url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    req = request.Request(url, method="GET")
    try:
        with request.urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except URLError as exc:
        raise ValueError(f"Could not fetch OIDC metadata for issuer: {issuer}") from exc


def exchange_oidc_code(
    *,
    token_endpoint: str,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> dict:
    payload = parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        }
    ).encode("utf-8")
    req = request.Request(
        token_endpoint,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_oidc_userinfo(*, userinfo_endpoint: str, access_token: str) -> dict:
    req = request.Request(
        userinfo_endpoint,
        headers={"Authorization": f"Bearer {access_token}"},
        method="GET",
    )
    with request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def validate_oidc_claims(
    *,
    provider: WorkspaceSSOProvider,
    id_token: str | None,
    userinfo: dict | None,
) -> dict:
    claims: dict = {}
    if id_token:
        try:
            claims = jwt.get_unverified_claims(id_token)
        except JWTError as exc:
            raise ValueError("Invalid id_token claims") from exc
    elif userinfo:
        claims = dict(userinfo)
    else:
        raise ValueError("Missing OIDC claims")

    issuer = str(claims.get("iss") or "").rstrip("/")
    expected_issuer = provider.issuer.rstrip("/")
    if issuer and issuer != expected_issuer:
        raise ValueError("OIDC issuer mismatch")

    aud = claims.get("aud")
    audiences: list[str]
    if isinstance(aud, str):
        audiences = [aud]
    elif isinstance(aud, list):
        audiences = [str(x) for x in aud]
    else:
        audiences = []
    if audiences and provider.client_id not in audiences:
        raise ValueError("OIDC audience mismatch")

    if "email_verified" in claims and claims.get("email_verified") is False:
        raise ValueError("OIDC email is not verified")
    return claims
