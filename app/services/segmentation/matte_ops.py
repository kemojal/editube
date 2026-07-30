"""Matte refinement — invert, grow/shrink, feather, strength.

One implementation, used by both the interactive preview and the export. That is
the whole point: the preview endpoint runs this on a single frame and the job runs
it on every frame, so what the user tunes on screen is arithmetically what they
get in the render. A second implementation on the client would be a WYSIWYG bug
waiting to happen, which is the same reasoning as `chroma_key.py`.

Radii are fractions of the frame's **shorter edge**, not pixels. A 6% feather has
to look the same on a 720p clip and a 4K one, and it has to survive the user
changing the composition afterwards — the same reason the selection and mask
models are stored normalised.

Order of operations is deliberate and worth stating, because it is the part that
would otherwise get quietly changed:

    grow/shrink  ->  feather  ->  invert  ->  strength

Geometry before softening, so growing does not just smear an already-blurred
edge. Invert *after* both, so "grow" always means "grow the subject the user
selected" whether or not the result is inverted — if invert came first, the
control would reverse its meaning and feel broken.
"""

from __future__ import annotations

from typing import Any

#: Largest grow/shrink, as a fraction of the shorter edge. Past this the
#: morphology dominates the shape rather than adjusting it.
MAX_EXPAND = 0.10

#: Largest feather, same units.
MAX_FEATHER = 0.10


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _to_float(value: Any, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    # NaN fails this comparison, which is the point.
    return number if number == number else fallback


def matte_settings_from_attributes(remove_bg: dict[str, Any] | None) -> dict[str, Any]:
    """Reads refinement settings out of a clip's stored `removeBg`.

    `??`-style handling rather than truthiness throughout: 0 is a meaningful value
    for every one of these (no feather, no growth), and treating it as "unset"
    would silently re-apply a default the user had deliberately turned off. That
    exact bug already bit the chroma key across the two languages.
    """
    source = remove_bg or {}
    return {
        "invert": bool(source.get("invertMask") or False),
        "expand": _clamp(_to_float(source.get("maskExpand"), 0.0), -MAX_EXPAND, MAX_EXPAND),
        "feather": _clamp(_to_float(source.get("maskFeather"), 0.0), 0.0, MAX_FEATHER),
        "opacity": _clamp(_to_float(source.get("maskOpacity"), 1.0), 0.0, 1.0),
    }


def is_identity(settings: dict[str, Any]) -> bool:
    """True when refinement would change nothing.

    Checked per clip, not per frame, so an untouched matte skips a dilate and a
    blur on every frame of the clip rather than paying for a no-op.
    """
    return (
        not settings.get("invert")
        and abs(_to_float(settings.get("expand"), 0.0)) < 1e-9
        and _to_float(settings.get("feather"), 0.0) < 1e-9
        and abs(_to_float(settings.get("opacity"), 1.0) - 1.0) < 1e-9
    )


def _odd_kernel(radius_px: int) -> int:
    """Morphology and Gaussian kernels must be odd and at least 3 to do anything."""
    size = max(1, int(round(radius_px))) * 2 + 1
    return size if size % 2 == 1 else size + 1


def refine_matte(matte, settings: dict[str, Any]):
    """Applies refinement to a uint8 matte and returns a new uint8 matte.

    `matte` is single-channel 0..255, where 255 is "keep". Returns the same shape
    and dtype so callers can treat this as a pass-through.
    """
    import cv2  # type: ignore
    import numpy as np  # type: ignore

    if is_identity(settings):
        return matte

    height, width = matte.shape[:2]
    shorter = max(1, min(height, width))

    result = matte

    # 1. Grow / shrink. Dilate grows the kept region, erode shrinks it — which is
    #    why this has to happen before any inversion, or the sign would flip.
    expand = _clamp(_to_float(settings.get("expand"), 0.0), -MAX_EXPAND, MAX_EXPAND)
    if abs(expand) > 1e-9:
        radius_px = abs(expand) * shorter
        if radius_px >= 0.5:
            kernel = cv2.getStructuringElement(
                cv2.MORPH_ELLIPSE, (_odd_kernel(radius_px), _odd_kernel(radius_px))
            )
            result = cv2.dilate(result, kernel) if expand > 0 else cv2.erode(result, kernel)

    # 2. Feather. A Gaussian on the matte turns a hard cut into a ramp, which is
    #    what stops a cutout looking like it was pasted on.
    feather = _clamp(_to_float(settings.get("feather"), 0.0), 0.0, MAX_FEATHER)
    if feather > 1e-9:
        radius_px = feather * shorter
        if radius_px >= 0.5:
            size = _odd_kernel(radius_px)
            result = cv2.GaussianBlur(result, (size, size), 0)

    # 3. Invert. Last of the shape operations so every control above keeps
    #    referring to the subject the user selected.
    if settings.get("invert"):
        result = 255 - result

    # 4. Strength. Scales the whole matte, so the "removed" area becomes partly
    #    visible rather than fully transparent — a softer composite, and the only
    #    one of these that is not about the edge.
    opacity = _clamp(_to_float(settings.get("opacity"), 1.0), 0.0, 1.0)
    if abs(opacity - 1.0) > 1e-9:
        result = (result.astype(np.float32) * opacity).clip(0, 255).astype(np.uint8)

    # Morphology and blur preserve dtype, but the float path above does not, and a
    # caller feeding this to ffmpeg's alphamerge needs uint8 either way.
    return result if result.dtype == np.uint8 else result.astype(np.uint8)
