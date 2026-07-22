"""Per-platform export presets for UGC ads.

Drives campaign defaults, the variation builder's allowed options, and the
length cap applied during fan-out. Disclosure guidance lives in
``ugc_compliance.platform_guidance``.
"""

from __future__ import annotations

from typing import Any

PLATFORM_PRESETS: dict[str, dict[str, Any]] = {
    "tiktok": {
        "key": "tiktok",
        "label": "TikTok",
        "aspect_ratio": "9:16",
        "allowed_aspect_ratios": ["9:16"],
        "lengths": [15, 30, 45],
        "max_length_sec": 60,
        "tip": "Hook in the first 2s; native, hand-held feel; captions on.",
    },
    "reels": {
        "key": "reels",
        "label": "Instagram Reels",
        "aspect_ratio": "9:16",
        "allowed_aspect_ratios": ["9:16"],
        "lengths": [15, 30],
        "max_length_sec": 90,
        "tip": "Keep safe margins clear of the right-side action rail.",
    },
    "shorts": {
        "key": "shorts",
        "label": "YouTube Shorts",
        "aspect_ratio": "9:16",
        "allowed_aspect_ratios": ["9:16"],
        "lengths": [15, 30, 45],
        "max_length_sec": 60,
        "tip": "Front-load the payoff; Shorts must be ≤ 60s.",
    },
    "meta": {
        "key": "meta",
        "label": "Meta Ads",
        "aspect_ratio": "9:16",
        "allowed_aspect_ratios": ["9:16", "1:1", "16:9"],
        "lengths": [15, 30],
        "max_length_sec": 60,
        "tip": "Test 9:16 for Stories/Reels and 1:1 for feed placements.",
    },
}

_DEFAULT = "tiktok"


def get_preset(platform: str | None) -> dict[str, Any]:
    return PLATFORM_PRESETS.get((platform or "").lower(), PLATFORM_PRESETS[_DEFAULT])


def list_presets() -> list[dict[str, Any]]:
    return list(PLATFORM_PRESETS.values())


def max_length_for(platform: str | None) -> int:
    return int(get_preset(platform).get("max_length_sec", 60))
