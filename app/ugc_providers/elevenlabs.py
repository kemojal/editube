"""ElevenLabs voice provider adapter (scaffold).

Standalone TTS for when voice is decoupled from the avatar engine. Wire the
real endpoint where marked, then select with ``UGC_VOICE_PROVIDER=elevenlabs``
+ ``ELEVENLABS_API_KEY``.
"""

from __future__ import annotations

import os

from app.ugc_providers.base import VoiceProvider, VoiceSpec

_API = "https://api.elevenlabs.io"


class ElevenLabsVoiceProvider(VoiceProvider):
    name = "elevenlabs"

    def __init__(self) -> None:
        self.api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
        if not self.api_key:
            raise RuntimeError("ELEVENLABS_API_KEY is not set (UGC_VOICE_PROVIDER=elevenlabs)")

    def list_voices(self) -> list[VoiceSpec]:
        # TODO: GET {_API}/v1/voices → map to VoiceSpec
        return []

    def synthesize(self, *, text: str, voice_id: str, out_path: str) -> str:
        # TODO: POST {_API}/v1/text-to-speech/{voice_id} → write audio bytes to out_path
        raise NotImplementedError(
            "ElevenLabs adapter not yet wired — set UGC_VOICE_PROVIDER=stub for now"
        )
