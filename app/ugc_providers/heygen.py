"""HeyGen avatar provider — real integration.

Implements the AvatarProvider contract against HeyGen's v2 generate + v1 status
APIs. Select with ``UGC_AVATAR_PROVIDER=heygen`` and ``HEYGEN_API_KEY``. Renders
are asynchronous: ``start_render`` returns a ``video_id`` and ``poll`` reports
status until ``completed`` with a downloadable URL. The UGC render job then
scales/crops, overlays captions + disclosure, and uploads.

Docs: https://docs.heygen.com (v2 /video/generate, v1 /video_status.get).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from app.ugc_providers.base import (
    AvatarProvider,
    AvatarSpec,
    ProviderJob,
    ProviderJobStatus,
    VoiceSpec,
)

logger = logging.getLogger(__name__)

_API = os.environ.get("HEYGEN_API_BASE", "https://api.heygen.com").rstrip("/")
_DIM = {"9:16": (720, 1280), "1:1": (1080, 1080), "16:9": (1280, 720)}

# HeyGen status → our normalized status.
_STATUS = {
    "completed": "done",
    "success": "done",
    "failed": "failed",
    "error": "failed",
    "processing": "processing",
    "pending": "processing",
    "waiting": "processing",
}


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


class HeyGenAvatarProvider(AvatarProvider):
    name = "heygen"

    def __init__(self) -> None:
        self.api_key = os.environ.get("HEYGEN_API_KEY", "").strip()
        if not self.api_key:
            raise RuntimeError("HEYGEN_API_KEY is not set (UGC_AVATAR_PROVIDER=heygen)")
        self.test_mode = _truthy(os.environ.get("HEYGEN_TEST_MODE"))
        self._default_voice: str | None = None

    # --- http helpers ---
    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self.api_key, "Content-Type": "application/json", "Accept": "application/json"}

    def _get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0)) as c:
            r = c.get(f"{_API}{path}", headers=self._headers(), params=params)
            return self._unwrap(r)

    def _post(self, path: str, body: dict) -> dict[str, Any]:
        with httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0)) as c:
            r = c.post(f"{_API}{path}", headers=self._headers(), json=body)
            return self._unwrap(r)

    @staticmethod
    def _unwrap(r: httpx.Response) -> dict[str, Any]:
        if r.status_code >= 400:
            raise RuntimeError(f"HeyGen API {r.status_code}: {r.text[:500]}")
        payload = r.json() if r.content else {}
        # v2 wraps in {"data": ..., "error": ...}; surface explicit errors.
        err = payload.get("error") if isinstance(payload, dict) else None
        if err:
            raise RuntimeError(f"HeyGen error: {err}")
        return payload if isinstance(payload, dict) else {}

    # --- catalog ---
    def list_avatars(self) -> list[AvatarSpec]:
        data = (self._get("/v2/avatars").get("data") or {})
        out: list[AvatarSpec] = []
        for a in data.get("avatars", []) or []:
            aid = a.get("avatar_id")
            if not aid:
                continue
            out.append(
                AvatarSpec(
                    provider_avatar_id=str(aid),
                    name=str(a.get("avatar_name") or aid),
                    thumbnail_url=a.get("preview_image_url"),
                    gender_presentation=a.get("gender"),
                )
            )
        return out

    def list_voices(self) -> list[VoiceSpec]:
        data = (self._get("/v2/voices").get("data") or {})
        out: list[VoiceSpec] = []
        for v in data.get("voices", []) or []:
            vid = v.get("voice_id")
            if not vid:
                continue
            out.append(
                VoiceSpec(
                    provider_voice_id=str(vid),
                    name=str(v.get("name") or vid),
                    gender=v.get("gender"),
                    language=v.get("language") or "en",
                    preview_url=v.get("preview_audio"),
                )
            )
        return out

    def _default_voice_id(self) -> str:
        if self._default_voice:
            return self._default_voice
        voices = self.list_voices()
        # Prefer an English voice; otherwise take the first available.
        chosen = next((v for v in voices if (v.language or "").lower().startswith("en")), None) or (
            voices[0] if voices else None
        )
        if not chosen:
            raise RuntimeError("HeyGen returned no voices to render with")
        self._default_voice = chosen.provider_voice_id
        return self._default_voice

    # --- render ---
    def start_render(
        self,
        *,
        script: str,
        avatar_id: str,
        voice_id: str,
        aspect_ratio: str = "9:16",
        length_sec: int = 30,
        options: dict | None = None,
    ) -> ProviderJob:
        if not avatar_id:
            raise RuntimeError("HeyGen render requires an avatar_id")
        vid = voice_id or self._default_voice_id()
        w, h = _DIM.get(aspect_ratio, _DIM["9:16"])
        body = {
            "video_inputs": [
                {
                    "character": {"type": "avatar", "avatar_id": avatar_id, "avatar_style": "normal"},
                    "voice": {"type": "text", "input_text": script or "", "voice_id": vid},
                }
            ],
            "dimension": {"width": w, "height": h},
            "test": self.test_mode,
        }
        payload = self._post("/v2/video/generate", body)
        data = payload.get("data") or {}
        video_id = data.get("video_id") or payload.get("video_id")
        if not video_id:
            raise RuntimeError(f"HeyGen did not return a video_id: {str(payload)[:300]}")
        return ProviderJob(provider_job_id=str(video_id), status="processing")

    def poll(self, provider_job_id: str) -> ProviderJobStatus:
        payload = self._get("/v1/video_status.get", params={"video_id": provider_job_id})
        data = payload.get("data") or {}
        raw = str(data.get("status") or "").lower()
        status = _STATUS.get(raw, "processing")
        if status == "done":
            url = data.get("video_url") or data.get("video_url_caption")
            if not url:
                # Reported complete but URL not propagated yet — keep polling.
                return ProviderJobStatus(status="processing", progress=95)
            return ProviderJobStatus(status="done", video_url=url, progress=100)
        if status == "failed":
            err = data.get("error") or data.get("msg") or "HeyGen render failed"
            return ProviderJobStatus(status="failed", error=str(err))
        return ProviderJobStatus(status="processing", progress=50)

    def supports_webhook(self) -> bool:
        return True
