"""Live capability probes for the editing harness.

Four probe endpoints already existed (segmentation, director, media providers,
model catalog) but retouch, audio enhancement, mask tracking, and export had
none — they failed at job time (plan §5.2 G9). This module is the aggregate:
every capability reports `available`, a human `reason` when it is not, its
provider identity, and its hard limits, so the planner can refuse an operation
before a worker discovers the same thing minutes later.

Probes are defensive by construction: a probe may return "unavailable", it may
never raise. Snapshots are stored on each run so a later failure can be told
apart from environmental drift.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from typing import Any

SNAPSHOT_VERSION = 1

#: Hard limits mirrored from the runtimes that enforce them.
SEGMENTATION_MAX_CLIP_SECONDS_DEFAULT = 120
RETOUCH_MAX_CLIP_SECONDS_DEFAULT = 180


def _entry(
    key: str,
    available: bool,
    *,
    reason: str | None = None,
    provider: str | None = None,
    limits: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {"key": key, "available": bool(available)}
    if not available and reason:
        entry["reason"] = reason
    if provider:
        entry["provider"] = provider
    if limits:
        entry["limits"] = limits
    if detail:
        entry["detail"] = detail
    return entry


def probe_segmentation() -> dict[str, Any]:
    try:
        from app.services.segmentation import get_provider
        from app.services.segmentation.base import (
            CAPABILITY_AUTO_MATTE,
            CAPABILITY_POINT_PROMPT,
            CAPABILITY_PROPAGATE,
        )

        provider = get_provider()
        ready, reason = provider.is_available()
        max_seconds = int(
            os.environ.get("SEGMENTATION_LOCAL_MAX_SECONDS", "")
            or SEGMENTATION_MAX_CLIP_SECONDS_DEFAULT
        )
        return _entry(
            "segmentation",
            bool(ready),
            reason=reason or None,
            provider=getattr(provider, "name", None),
            limits={"maxClipSeconds": max_seconds},
            detail={
                "autoMatte": provider.supports(CAPABILITY_AUTO_MATTE),
                "pointPrompt": provider.supports(CAPABILITY_POINT_PROMPT),
                "propagate": provider.supports(CAPABILITY_PROPAGATE),
            },
        )
    except Exception as exc:  # noqa: BLE001 — probes never raise
        return _entry("segmentation", False, reason=f"Probe failed: {exc}")


def probe_tracking() -> dict[str, Any]:
    """Box tracking. Honest by design: OpenCV importing is not a tracker.

    The pinned `opencv-python-headless` build has no CSRT (contrib-only), so
    `mask_track_job` dies on AttributeError today (plan §5.2 G7). This probe
    checks the attribute itself and reports SAM2 propagation as the available
    substitute when torch+sam2 are installed.
    """
    backend: str | None = None
    reason: str | None = None
    try:
        # One source of truth with the job itself: whatever the probe reports
        # is exactly what `mask_track_job` will decide when it runs.
        from app.jobs.mask_track import tracker_availability

        backend, reason = tracker_availability()
    except Exception:  # noqa: BLE001
        reason = "OpenCV is not importable in this environment."
    csrt = backend == "csrt"

    propagate = False
    try:
        from app.services.segmentation import get_provider
        from app.services.segmentation.base import CAPABILITY_PROPAGATE

        propagate = get_provider().supports(CAPABILITY_PROPAGATE)
    except Exception:  # noqa: BLE001
        propagate = False

    available = backend is not None or propagate
    provider = (
        "opencv-csrt"
        if csrt
        else "opencv-mil"
        if backend == "mil"
        else "sam2-propagate"
        if propagate
        else None
    )
    return _entry(
        "tracking",
        available,
        reason=None if available else (reason or "No tracking backend available."),
        provider=provider,
        detail={"csrt": csrt, "boxTracker": backend, "maskPropagation": propagate},
    )


def probe_retouch() -> dict[str, Any]:
    try:
        import cv2  # noqa: F401, PLC0415
    except Exception:  # noqa: BLE001
        return _entry("retouch", False, reason="OpenCV is not importable in this environment.")
    try:
        from app.services.retouch.beauty import YUNET_MODEL_PATH  # type: ignore[attr-defined]

        model_path = YUNET_MODEL_PATH
    except Exception:  # noqa: BLE001
        from pathlib import Path

        model_path = (
            Path(__file__).resolve().parents[2]
            / "assets"
            / "models"
            / "face_detection_yunet_2023mar.onnx"
        )
    from pathlib import Path as _Path

    has_model = _Path(str(model_path)).exists()
    max_seconds = int(
        os.environ.get("RETOUCH_LOCAL_MAX_SECONDS", "") or RETOUCH_MAX_CLIP_SECONDS_DEFAULT
    )
    return _entry(
        "retouch",
        True,
        provider="opencv-yunet" if has_model else "opencv-haar",
        limits={"maxClipSeconds": max_seconds},
        detail={"yunetModel": has_model},
    )


def probe_audio_enhance() -> dict[str, Any]:
    mode = (os.environ.get("AUDIO_ENHANCE_PROVIDER") or "auto").strip().lower()
    if mode == "http":
        url = (os.environ.get("AUDIO_ENHANCE_URL") or "").strip()
        return _entry(
            "audio_enhance",
            bool(url),
            reason=None if url else "AUDIO_ENHANCE_URL is not configured.",
            provider="http",
        )
    demucs = False
    try:
        import importlib.util

        demucs = importlib.util.find_spec("demucs") is not None
    except Exception:  # noqa: BLE001
        demucs = False
    ffmpeg = shutil.which(os.environ.get("FFMPEG_PATH") or "ffmpeg") is not None
    provider = "demucs+deepfilternet" if demucs else ("ffmpeg-fallback" if ffmpeg else None)
    return _entry(
        "audio_enhance",
        bool(provider),
        reason=None if provider else "Neither Demucs nor ffmpeg is available.",
        provider=provider,
        detail={"demucs": demucs, "ffmpeg": ffmpeg},
    )


def probe_generation() -> dict[str, Any]:
    try:
        from app.services.ai_media import provider_availability

        providers = provider_availability()
        available = any(bool(v) for v in providers.values())
        return _entry(
            "media_generation",
            available,
            reason=None if available else "No generation provider is configured.",
            detail={"providers": providers},
        )
    except Exception as exc:  # noqa: BLE001
        return _entry("media_generation", False, reason=f"Probe failed: {exc}")


def _ffmpeg_has_filter(binary: str, name: str) -> bool:
    import subprocess

    try:
        listing = subprocess.run(
            [binary, "-hide_banner", "-filters"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except Exception:  # noqa: BLE001
        return False
    return any(line.split()[1:2] == [name] for line in listing.splitlines())


def probe_export() -> dict[str, Any]:
    binary = os.environ.get("FFMPEG_PATH") or "ffmpeg"
    ffmpeg = shutil.which(binary) is not None
    # Not every ffmpeg is the ffmpeg the exporter needs: Homebrew's default
    # build ships WITHOUT libass, so caption burn-in fails at filter parse on
    # a box where `ffmpeg -version` looks perfectly healthy. Probe the filter
    # itself, not the binary's existence.
    subtitles = _ffmpeg_has_filter(binary, "subtitles") if ffmpeg else False
    try:
        from app.utils.cloudinary import cloudinary_credentials_configured

        cloudinary = bool(cloudinary_credentials_configured())
    except Exception:  # noqa: BLE001
        cloudinary = False
    available = ffmpeg and cloudinary
    reason = None
    if not ffmpeg:
        reason = "ffmpeg is not on PATH for this process."
    elif not cloudinary:
        reason = "Cloudinary is not configured; the exporter cannot publish output."
    return _entry(
        "export",
        available,
        reason=reason,
        detail={
            "ffmpeg": ffmpeg,
            "cloudinary": cloudinary,
            "subtitlesFilter": subtitles,
            **(
                {}
                if subtitles or not ffmpeg
                else {
                    "subtitlesFilterNote": (
                        "This ffmpeg build has no libass subtitles filter; "
                        "caption burn-in will fail. Install an ffmpeg built "
                        "with --enable-libass."
                    )
                }
            ),
        },
    )


def probe_queue() -> dict[str, Any]:
    url = (os.environ.get("REDIS_URL") or "").strip()
    if not url:
        return _entry("queue", False, reason="REDIS_URL is not configured; jobs cannot run.")
    try:
        from redis import Redis
        from rq import Worker

        conn = Redis.from_url(url)
        conn.ping()
        workers = Worker.count(connection=conn)
        return _entry(
            "queue",
            workers > 0,
            reason=None if workers > 0 else "Redis is reachable but no worker is connected.",
            detail={"workers": int(workers)},
        )
    except Exception as exc:  # noqa: BLE001
        return _entry("queue", False, reason=f"Redis is not reachable: {exc}")


def probe_storage() -> dict[str, Any]:
    try:
        from app.storage import storage_available

        ok = bool(storage_available())
        return _entry(
            "storage",
            ok,
            reason=None if ok else "No storage backend is configured.",
            provider=(os.environ.get("STORAGE_BACKEND") or "cloudinary").strip() or None,
        )
    except Exception as exc:  # noqa: BLE001
        return _entry("storage", False, reason=f"Probe failed: {exc}")


def probe_fonts() -> dict[str, Any]:
    """Server-side text rendering. Today that is exactly one family (mask text)."""
    try:
        from app.services.mask_text import MASK_FONT_DIR, MASK_FONTS

        families = sorted(MASK_FONTS.keys())
        present = MASK_FONT_DIR.exists()
        return _entry(
            "server_fonts",
            present and bool(families),
            reason=None if present else "Mask font directory is missing on this worker.",
            detail={"families": families},
        )
    except Exception as exc:  # noqa: BLE001
        return _entry("server_fonts", False, reason=f"Probe failed: {exc}")


def snapshot() -> dict[str, Any]:
    """The full capability snapshot, stored verbatim on every run."""
    entries = [
        probe_queue(),
        probe_storage(),
        probe_export(),
        probe_segmentation(),
        probe_tracking(),
        probe_retouch(),
        probe_audio_enhance(),
        probe_generation(),
        probe_fonts(),
    ]
    return {
        "version": SNAPSHOT_VERSION,
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "capabilities": {entry["key"]: entry for entry in entries},
    }


def capability(snapshot_dict: dict[str, Any] | None, key: str) -> dict[str, Any]:
    caps = (snapshot_dict or {}).get("capabilities") or {}
    entry = caps.get(key)
    return entry if isinstance(entry, dict) else {"key": key, "available": False, "reason": "Not probed."}
