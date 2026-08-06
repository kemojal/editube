"""Retouch settings validation and adaptive auto-retouch defaults."""

from __future__ import annotations

import math
from typing import Any


SLIDER_KEYS = (
    "skinSmooth",
    "blemishRemoval",
    "evenTone",
    "skinBrighten",
    "glow",
    "teethWhiten",
    "eyeBrighten",
    "darkCircles",
    "smileLines",
    "faceSlim",
    "jawSculpt",
    "eyeSize",
    "noseSlim",
    "chinShape",
    "lipColor",
    "blush",
)

AUTO_BASE = {
    "skinSmooth": 32.0,
    "blemishRemoval": 24.0,
    "evenTone": 22.0,
    "skinBrighten": 10.0,
    "glow": 8.0,
    "teethWhiten": 18.0,
    "eyeBrighten": 12.0,
    "darkCircles": 16.0,
    "smileLines": 8.0,
}


def _number(value: Any, fallback: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if math.isfinite(number) else fallback


def sanitize_retouch_settings(raw: Any) -> dict[str, Any]:
    value = raw if isinstance(raw, dict) else {}
    settings: dict[str, Any] = {
        "enabled": value.get("enabled") is not False,
        "autoRetouch": bool(value.get("autoRetouch") or value.get("autoStyles")),
        "autoAmount": max(0.0, min(100.0, _number(value.get("autoAmount"), 55.0))),
        "detailProtection": max(0.0, min(100.0, _number(value.get("detailProtection"), 70.0))),
        "targetFaces": "primary" if value.get("targetFaces") == "primary" else "all",
    }
    for key in SLIDER_KEYS:
        settings[key] = max(0.0, min(100.0, _number(value.get(key), 0.0)))
    return settings


def effective_retouch_settings(raw: Any, *, face_luma: float | None = None) -> dict[str, Any]:
    settings = sanitize_retouch_settings(raw)
    if not settings["autoRetouch"]:
        return settings
    amount = settings["autoAmount"] / 100.0
    adaptive = dict(AUTO_BASE)
    if face_luma is not None:
        # Darker faces receive a small exposure lift; well-lit faces do not get
        # washed out. This is the part that makes Auto adaptive rather than a
        # disguised fixed preset.
        adaptive["skinBrighten"] += max(-4.0, min(14.0, (138.0 - face_luma) * 0.13))
        adaptive["glow"] += max(0.0, min(8.0, (150.0 - face_luma) * 0.05))
    for key, baseline in adaptive.items():
        settings[key] = max(settings[key], max(0.0, baseline * amount))
    return settings


def has_retouch_adjustments(raw: Any) -> bool:
    settings = sanitize_retouch_settings(raw)
    return bool(
        settings["enabled"]
        and (
            settings["autoRetouch"]
            or any(float(settings[key]) > 0.001 for key in SLIDER_KEYS)
        )
    )
