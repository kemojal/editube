"""Creatify avatar provider adapter (scaffold).

Product-link-native AI UGC. Implements the AvatarProvider contract; wire the
real Creatify endpoints where marked, then select with
``UGC_AVATAR_PROVIDER=creatify`` + ``CREATIFY_API_KEY`` / ``CREATIFY_API_ID``.
"""

from __future__ import annotations

import os

from app.ugc_providers.base import (
    AvatarProvider,
    AvatarSpec,
    ProviderJob,
    ProviderJobStatus,
    VoiceSpec,
)

_API = "https://api.creatify.ai"


class CreatifyAvatarProvider(AvatarProvider):
    name = "creatify"

    def __init__(self) -> None:
        self.api_key = os.environ.get("CREATIFY_API_KEY", "").strip()
        self.api_id = os.environ.get("CREATIFY_API_ID", "").strip()
        if not self.api_key:
            raise RuntimeError("CREATIFY_API_KEY is not set (UGC_AVATAR_PROVIDER=creatify)")

    def _headers(self) -> dict:
        return {"X-API-ID": self.api_id, "X-API-KEY": self.api_key, "Content-Type": "application/json"}

    def list_avatars(self) -> list[AvatarSpec]:
        # TODO: GET {_API}/api/personas/ → map to AvatarSpec
        return []

    def list_voices(self) -> list[VoiceSpec]:
        # TODO: GET {_API}/api/voices/ → map to VoiceSpec
        return []

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
        # TODO: POST {_API}/api/lipsyncs/ (or link_to_videos) → ProviderJob(id, processing)
        raise NotImplementedError(
            "Creatify adapter not yet wired — set UGC_AVATAR_PROVIDER=stub for now"
        )

    def poll(self, provider_job_id: str) -> ProviderJobStatus:
        # TODO: GET {_API}/api/lipsyncs/{id}/ → map status + output URL
        raise NotImplementedError("Creatify adapter not yet wired")

    def supports_webhook(self) -> bool:
        return True
