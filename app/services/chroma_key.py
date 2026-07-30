"""Chroma key filter construction for export.

The Python twin of `_lib/chroma/chroma-key.ts`. Two implementations exist
because the preview runs in the browser and the render runs here, and the only
thing keeping them honest is a shared golden fixture — see
`tests/fixtures/chroma_key_cases.json` and the contract test beside it, the same
arrangement already used for the mask export.

Ranges are 0..1 to match ffmpeg's `chromakey`, which is the side neither
implementation gets to change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEX = re.compile(r"^([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

DEFAULT_SIMILARITY = 0.4
DEFAULT_BLEND = 0.1
DEFAULT_SPILL = 0.0
DEFAULT_COLOR = "#00ff00"


@dataclass(frozen=True)
class ChromaKeySettings:
    enabled: bool = False
    color: str = DEFAULT_COLOR
    similarity: float = DEFAULT_SIMILARITY
    blend: float = DEFAULT_BLEND
    spill: float = DEFAULT_SPILL


def parse_hex_color(value: str) -> tuple[int, int, int] | None:
    """Parses `#rgb` / `#rrggbb`. Returns None rather than guessing."""
    if not isinstance(value, str):
        return None
    text = value.strip().lstrip("#")
    if not _HEX.match(text):
        return None
    if len(text) == 3:
        text = "".join(char * 2 for char in text)
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def _clamp01(value: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


def chroma_key_from_attributes(remove_bg: dict | None) -> ChromaKeySettings:
    """Reads the settings out of a clip's stored `removeBg` attributes.

    Mirrors `chromaKeyFromAttributes` on the frontend, including the detail that
    matters: `.get(k) or default` would replace a deliberate 0 — a hard-edge key
    sets blend to exactly 0 — so each field is checked for None instead.
    """
    data = remove_bg or {}

    def number(key: str, default: float) -> float:
        value = data.get(key)
        return _clamp01(value) if value is not None else default

    return ChromaKeySettings(
        enabled=bool(data.get("chromaKey", False)),
        color=data.get("keyColor") or DEFAULT_COLOR,
        similarity=number("similarity", DEFAULT_SIMILARITY),
        blend=number("blend", DEFAULT_BLEND),
        spill=number("spill", DEFAULT_SPILL),
    )


def build_chroma_key_filter(settings: ChromaKeySettings) -> str | None:
    """ffmpeg filter chain, or None when there is nothing to do.

    Returning None rather than an identity filter matters: an unused pass still
    costs a full decode/encode of every frame.
    """
    if not settings.enabled:
        return None

    rgb = parse_hex_color(settings.color)
    if rgb is None:
        return None

    hex_color = "%02x%02x%02x" % rgb
    chain = [
        "chromakey=0x%s:%.4f:%.4f"
        % (hex_color, _clamp01(settings.similarity), _clamp01(settings.blend))
    ]

    if settings.spill > 0:
        chain.append("despill=type=green:mix=%.4f" % _clamp01(settings.spill))

    return ",".join(chain)
