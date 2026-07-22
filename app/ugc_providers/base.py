"""Provider interfaces for AI UGC avatar + voice generation.

Mirrors ``app/publishers/`` — a thin contract with swappable implementations
selected by env (``UGC_AVATAR_PROVIDER`` / ``UGC_VOICE_PROVIDER``). Routes and
jobs never call a concrete provider directly; they go through
``get_avatar_provider`` / ``get_voice_provider``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class AvatarSpec:
    provider_avatar_id: str
    name: str
    thumbnail_url: Optional[str] = None
    age_range: Optional[str] = None
    gender_presentation: Optional[str] = None
    region: Optional[str] = None
    default_voice_id: Optional[str] = None
    accent: Optional[str] = None
    energy: Optional[str] = None
    is_premium: bool = False


@dataclass(frozen=True)
class VoiceSpec:
    provider_voice_id: str
    name: str
    gender: Optional[str] = None
    accent: Optional[str] = None
    language: str = "en"
    preview_url: Optional[str] = None
    is_premium: bool = False


@dataclass(frozen=True)
class ProviderJob:
    """Handle returned by ``start_render`` for an async provider render."""

    provider_job_id: str
    # Some providers (the stub) finish synchronously and can hand back a URL now.
    video_url: Optional[str] = None
    status: str = "processing"  # queued|processing|done|failed


@dataclass
class ProviderJobStatus:
    status: str  # queued|processing|done|failed
    video_url: Optional[str] = None
    error: Optional[str] = None
    progress: int = 0


class AvatarProvider(ABC):
    """Talking-head / AI-creator video generation backend."""

    name: str = "base"

    @abstractmethod
    def list_avatars(self) -> list[AvatarSpec]:
        raise NotImplementedError

    @abstractmethod
    def list_voices(self) -> list[VoiceSpec]:
        raise NotImplementedError

    @abstractmethod
    def start_render(
        self,
        *,
        script: str,
        avatar_id: str,
        voice_id: str,
        aspect_ratio: str = "9:16",
        length_sec: int = 30,
        options: Optional[dict] = None,
    ) -> ProviderJob:
        """Kick off an avatar render. Returns a job handle to poll."""
        raise NotImplementedError

    @abstractmethod
    def poll(self, provider_job_id: str) -> ProviderJobStatus:
        """Check render status; on ``done`` includes a downloadable ``video_url``."""
        raise NotImplementedError

    def supports_webhook(self) -> bool:
        return False


class VoiceProvider(ABC):
    """Standalone text-to-speech backend (when voice is decoupled from avatar)."""

    name: str = "base"

    @abstractmethod
    def list_voices(self) -> list[VoiceSpec]:
        raise NotImplementedError

    @abstractmethod
    def synthesize(self, *, text: str, voice_id: str, out_path: str) -> str:
        """Write audio to ``out_path``; return the path."""
        raise NotImplementedError
