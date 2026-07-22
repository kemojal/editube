"""AI UGC provider registry. Mirrors ``app/publishers``.

Resolution order: explicit ``name`` arg → ``UGC_AVATAR_PROVIDER`` /
``UGC_VOICE_PROVIDER`` env → ``stub``. ``UGC_RENDER_DRY_RUN`` forces the stub
regardless, so paid providers are never hit in tests/dev.
"""

from __future__ import annotations

import os

from app.ugc_providers.base import (
    AvatarProvider,
    AvatarSpec,
    ProviderJob,
    ProviderJobStatus,
    VoiceProvider,
    VoiceSpec,
)
from app.ugc_providers.stub import StubAvatarProvider, StubVoiceProvider


def _dry_run() -> bool:
    return os.environ.get("UGC_RENDER_DRY_RUN", "").strip().lower() in ("1", "true", "yes")


def get_avatar_provider(name: str | None = None) -> AvatarProvider:
    chosen = (name or os.environ.get("UGC_AVATAR_PROVIDER", "stub")).strip().lower()
    if _dry_run() or chosen in ("", "stub"):
        return StubAvatarProvider()
    if chosen == "heygen":
        from app.ugc_providers.heygen import HeyGenAvatarProvider

        return HeyGenAvatarProvider()
    if chosen == "creatify":
        from app.ugc_providers.creatify import CreatifyAvatarProvider

        return CreatifyAvatarProvider()
    return StubAvatarProvider()


def get_voice_provider(name: str | None = None) -> VoiceProvider:
    chosen = (name or os.environ.get("UGC_VOICE_PROVIDER", "stub")).strip().lower()
    if _dry_run() or chosen in ("", "stub"):
        return StubVoiceProvider()
    if chosen == "elevenlabs":
        from app.ugc_providers.elevenlabs import ElevenLabsVoiceProvider

        return ElevenLabsVoiceProvider()
    return StubVoiceProvider()


__all__ = [
    "AvatarProvider",
    "VoiceProvider",
    "AvatarSpec",
    "VoiceSpec",
    "ProviderJob",
    "ProviderJobStatus",
    "StubAvatarProvider",
    "StubVoiceProvider",
    "get_avatar_provider",
    "get_voice_provider",
]
