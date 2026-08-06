"""Vectorised chroma-key matte used when keying is stacked with AI removal."""

from __future__ import annotations

from typing import Any

from app.services.chroma_key import chroma_key_from_attributes, parse_hex_color


MAX_CHROMA_DISTANCE = (127.5**2 + 127.5**2) ** 0.5


def chroma_keep_matte(frame_bgr: Any, settings: dict[str, Any]):
    """Returns the same 0..255 keep-alpha as the browser/ffmpeg keyer."""
    import numpy as np  # type: ignore

    key = chroma_key_from_attributes(settings)
    rgb = parse_hex_color(key.color)
    height, width = frame_bgr.shape[:2]
    if not key.enabled or rgb is None:
        return np.full((height, width), 255, dtype=np.uint8)

    pixels = frame_bgr[:, :, ::-1].astype(np.float32)
    r, g, b = pixels[:, :, 0], pixels[:, :, 1], pixels[:, :, 2]
    pixel_u = -0.169 * r - 0.331 * g + 0.5 * b
    pixel_v = 0.5 * r - 0.419 * g - 0.081 * b

    kr, kg, kb = rgb
    key_u = -0.169 * kr - 0.331 * kg + 0.5 * kb
    key_v = 0.5 * kr - 0.419 * kg - 0.081 * kb
    distance = np.hypot(pixel_u - key_u, pixel_v - key_v) / MAX_CHROMA_DISTANCE

    threshold = max(0.0, min(1.0, key.similarity))
    blend = max(0.0, min(1.0, key.blend))
    if blend <= 0:
        alpha = (distance > threshold + 1e-6).astype(np.float32)
    else:
        alpha = ((distance - threshold) / blend).clip(0.0, 1.0)
        alpha[distance <= threshold + 1e-6] = 0.0
    return (alpha * 255.0).round().astype(np.uint8)


def combine_keep_mattes(first: Any, second: Any):
    """Alpha multiplication, preserving soft edges from both mattes."""
    import numpy as np  # type: ignore

    return (
        first.astype(np.float32) * second.astype(np.float32) / 255.0
    ).round().clip(0, 255).astype(np.uint8)
