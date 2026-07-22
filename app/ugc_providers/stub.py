"""Zero-cost stub avatar/voice provider.

Drives the entire UGC pipeline end-to-end without any external account or
spend. ``start_render`` synchronously renders a deterministic placeholder MP4
(solid colour + silent track, sized to the target aspect) so the downstream
assembly + Cloudinary upload run for real. Used by default and whenever
``UGC_RENDER_DRY_RUN`` is set.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import uuid
from pathlib import Path

from app.ugc_providers.base import (
    AvatarProvider,
    AvatarSpec,
    ProviderJob,
    ProviderJobStatus,
    VoiceProvider,
    VoiceSpec,
)

logger = logging.getLogger(__name__)

STUB_OUTPUT_DIR = Path(os.environ.get("UGC_STUB_DIR", "./uploads/ugc/_stub")).resolve()

_RES = {"9:16": "720x1280", "1:1": "1080x1080", "16:9": "1280x720"}

_AVATARS = [
    AvatarSpec("ugc_f_us_1", "Maya (US, energetic)", age_range="18-24", gender_presentation="female", region="US", default_voice_id="v_f_us_warm", accent="American", energy="high"),
    AvatarSpec("ugc_f_uk_1", "Ella (UK, calm)", age_range="25-34", gender_presentation="female", region="UK", default_voice_id="v_f_uk_soft", accent="British", energy="medium"),
    AvatarSpec("ugc_m_us_1", "Jordan (US, founder)", age_range="25-34", gender_presentation="male", region="US", default_voice_id="v_m_us_direct", accent="American", energy="medium"),
    AvatarSpec("ugc_m_au_1", "Leo (AU, friendly)", age_range="18-24", gender_presentation="male", region="AU", default_voice_id="v_m_au_bright", accent="Australian", energy="high"),
    AvatarSpec("ugc_f_ca_1", "Nova (CA, lifestyle)", age_range="25-34", gender_presentation="female", region="CA", default_voice_id="v_f_us_warm", accent="Canadian", energy="medium", is_premium=True),
]

_VOICES = [
    VoiceSpec("v_f_us_warm", "Warm Female (US)", gender="female", accent="American"),
    VoiceSpec("v_f_uk_soft", "Soft Female (UK)", gender="female", accent="British"),
    VoiceSpec("v_m_us_direct", "Direct Male (US)", gender="male", accent="American"),
    VoiceSpec("v_m_au_bright", "Bright Male (AU)", gender="male", accent="Australian"),
]


def _color_for(avatar_id: str) -> str:
    digest = hashlib.sha1((avatar_id or "stub").encode()).hexdigest()
    return f"0x{digest[:6]}"


class StubAvatarProvider(AvatarProvider):
    name = "stub"

    def list_avatars(self) -> list[AvatarSpec]:
        return list(_AVATARS)

    def list_voices(self) -> list[VoiceSpec]:
        return list(_VOICES)

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
        STUB_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        job_id = uuid.uuid4().hex
        out = STUB_OUTPUT_DIR / f"{job_id}.mp4"
        size = _RES.get(aspect_ratio, _RES["9:16"])
        dur = max(3, min(int(length_sec or 30), 60))
        color = _color_for(avatar_id)
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c={color}:s={size}:d={dur}:r=30",
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t", str(dur),
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart",
            str(out),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "")[-800:]
            raise RuntimeError(f"stub avatar render (ffmpeg) failed: {tail}")
        return ProviderJob(provider_job_id=job_id, video_url=str(out), status="done")

    def poll(self, provider_job_id: str) -> ProviderJobStatus:
        out = STUB_OUTPUT_DIR / f"{provider_job_id}.mp4"
        if out.exists():
            return ProviderJobStatus(status="done", video_url=str(out), progress=100)
        return ProviderJobStatus(status="failed", error="stub render output missing")


class StubVoiceProvider(VoiceProvider):
    name = "stub"

    def list_voices(self) -> list[VoiceSpec]:
        return list(_VOICES)

    def synthesize(self, *, text: str, voice_id: str, out_path: str) -> str:
        # Silent track of a length proportional to the script — enough to mux.
        dur = max(3, min(len(text or "") // 12, 60))
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t", str(dur), "-c:a", "aac", "-b:a", "96k", out_path,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError("stub voice synthesize failed")
        return out_path
