"""Signed review media URLs and HTTP proxy for gated playback/download."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import AsyncIterator
from urllib.parse import quote

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from app.db.models import Video
from app.utils.security import SECRET_KEY

logger = logging.getLogger(__name__)

_PLAYBACK_TTL_SEC = int(os.getenv("REVIEW_MEDIA_PLAYBACK_TTL_SEC", "7200"))
_DOWNLOAD_TTL_SEC = int(os.getenv("REVIEW_MEDIA_DOWNLOAD_TTL_SEC", "600"))


def _signing_key() -> bytes:
    raw = os.getenv("REVIEW_MEDIA_SIGNING_SECRET") or SECRET_KEY or ""
    return raw.encode("utf-8")


def sign_review_media(token: str, session_id: int, purpose: str, exp: int) -> str:
    msg = f"{token}:{session_id}:{purpose}:{exp}".encode("utf-8")
    return hmac.new(_signing_key(), msg, hashlib.sha256).hexdigest()


def verify_review_media_sig(
    token: str, session_id: int, purpose: str, exp: int, sig: str
) -> bool:
    try:
        expected = sign_review_media(token, session_id, purpose, exp)
        return hmac.compare_digest(expected, sig)
    except Exception:
        return False


def media_expiry_seconds(purpose: str) -> int:
    return _DOWNLOAD_TTL_SEC if purpose == "download" else _PLAYBACK_TTL_SEC


def build_review_media_url(
    *,
    api_base: str,
    token: str,
    session_id: int,
    purpose: str,
) -> str:
    from datetime import datetime, timezone

    exp = int(datetime.now(timezone.utc).timestamp()) + media_expiry_seconds(purpose)
    sig = sign_review_media(token, session_id, purpose, exp)
    base = api_base.rstrip("/")
    q = (
        f"session_id={session_id}&purpose={purpose}&exp={exp}&sig={quote(sig, safe='')}"
    )
    return f"{base}/review/{token}/media?{q}"


def _local_media_path(file_path: str) -> str:
    fp = (file_path or "").strip()
    if not fp:
        return ""
    if fp.startswith("http://") or fp.startswith("https://"):
        return ""
    if os.path.isabs(fp):
        return fp
    return os.path.normpath(os.path.join(os.getcwd(), fp.lstrip("/")))


async def head_upstream_video(url: str) -> tuple[int, dict[str, str]]:
    """Lightweight HEAD for remote URLs (Range probing)."""
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
    ) as client:
        r = await client.head(url)
    keep = (
        "content-type",
        "content-length",
        "accept-ranges",
        "content-range",
    )
    headers = {
        k: v
        for k, v in r.headers.items()
        if k.lower() in keep and v
    }
    return r.status_code, headers


async def proxy_review_media(
    *,
    request: Request,
    video: Video,
    purpose: str,
) -> StreamingResponse | FileResponse:
    fp = (video.file_path or "").strip()
    if not fp:
        raise HTTPException(status_code=404, detail="Video file not configured")

    filename = quote((video.name or "review-video").replace("/", "-"), safe=".-_") + ".mp4"

    if fp.startswith("http://") or fp.startswith("https://"):
        req_headers: dict[str, str] = {}
        rng = request.headers.get("range")
        if rng:
            req_headers["Range"] = rng

        client = httpx.AsyncClient(
            timeout=httpx.Timeout(600.0, connect=30.0),
            follow_redirects=True,
        )
        try:
            upstream = await client.send(
                client.build_request("GET", fp, headers=req_headers),
                stream=True,
            )
        except httpx.RequestError as e:
            await client.aclose()
            logger.warning("Review media upstream error: %s", e)
            raise HTTPException(status_code=502, detail="Unable to reach video storage") from e

        if upstream.status_code not in (200, 206):
            await upstream.aclose()
            await client.aclose()
            raise HTTPException(
                status_code=upstream.status_code
                if 400 <= upstream.status_code < 600
                else 502,
                detail="Upstream rejected media request",
            )

        pass_headers = [
            "content-type",
            "content-length",
            "content-range",
            "accept-ranges",
            "etag",
            "last-modified",
        ]
        out_h = {
            k: v
            for k, v in upstream.headers.items()
            if k.lower() in pass_headers and v
        }
        if purpose == "download":
            out_h["content-disposition"] = f'attachment; filename="{filename}"'

        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_bytes(262_144):
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            body(),
            status_code=upstream.status_code,
            headers=out_h,
        )

    local_path = _local_media_path(fp)
    if not local_path or not os.path.isfile(local_path):
        raise HTTPException(status_code=404, detail="Video file not found on disk")

    headers = {}
    if purpose == "download":
        headers["content-disposition"] = f'attachment; filename="{filename}"'

    return FileResponse(
        local_path,
        media_type="video/mp4",
        headers=headers,
        filename=filename if purpose == "download" else None,
    )
