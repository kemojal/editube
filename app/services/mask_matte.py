"""Rasterises Task 12's mask geometry into a grayscale matte for MP4 export.

The browser preview draws masks as SVG. This module is the server-side
equivalent: it walks the same `mask_polygons` output and paints an `"L"`
(8-bit grayscale) Pillow image where white = keep, black = hide. Composed
per-frame mattes are then piped into ffmpeg as a raw grayscale video (never
a PNG sequence — a multi-minute clip at 30fps would produce thousands of
files on the worker's disk) and merged onto the exported frame via
`alphamerge` in `app/jobs/rough_cut_export.py`.

Security note: masks arrive straight from the request body
(`app/api/routes/ai.py` -> RQ job payload -> here). The frontend's
`sanitizeMasks` (TS) clamps numerics and caps array sizes before the editor
ever renders a mask; nothing enforces that on the way into this job, so
`sanitize_masks` below is the backend's independent copy of those limits.
Every mask reaching `render_matte_frame`/`render_matte_video` must go
through it first — an unbounded points array (or a NaN width) sent directly
to this endpoint would otherwise hang or crash the RQ worker.
"""

from __future__ import annotations

import logging
import math
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from app.services.mask_geometry import VIEWBOX, mask_is_inert, mask_polygons

logger = logging.getLogger(__name__)

# Mirrors the frontend's `sanitizeMasks` caps (see
# editube-frontend/.../_lib/mask/mask-sanitize.ts). Keep in sync if those
# change.
MAX_MASKS = 8
MAX_STROKES_PER_MASK = 200
MAX_POINTS_PER_STROKE = 4000  # flat [x0, y0, x1, y1, ...] pairs -> 2000 points
MAX_PATH_POINTS = 500
MAX_KEYFRAMES = 200

_VALID_OPS = {"add", "subtract", "intersect"}
_VALID_SHAPES = {
    "rectangle",
    "circle",
    "split",
    "filmstrip",
    "star",
    "heart",
    "text",
    "brush",
    "pen",
}


def _finite(value: Any, default: float = 0.0) -> float:
    """Coerces to a finite float; NaN/Inf/non-numeric fall back to `default`."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(f):
        return default
    return f


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _sanitize_keyframe(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    return {
        "t": _clamp(_finite(raw.get("t")), 0, 1_000_000),
        "x": _clamp(_finite(raw.get("x")), -1000, 1000),
        "y": _clamp(_finite(raw.get("y")), -1000, 1000),
        "width": _clamp(_finite(raw.get("width"), 1), 0, 1000),
        "height": _clamp(_finite(raw.get("height"), 1), 0, 1000),
        "rotation": _finite(raw.get("rotation")) % 360,
    }


def _sanitize_stroke(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    points = raw.get("points")
    if not isinstance(points, list):
        return None
    clean_points: list[float] = []
    for p in points[:MAX_POINTS_PER_STROKE]:
        clean_points.append(_clamp(_finite(p), -10, 110))
    # Points come as flat [x, y, x, y, ...] pairs; truncate to an even count
    # so downstream pairing (`points[i], points[i+1]`) never runs off a
    # dangling coordinate.
    if len(clean_points) % 2:
        clean_points.pop()
    return {
        "points": clean_points,
        "size": _clamp(_finite(raw.get("size"), 4), 0.1, 100),
        "erase": bool(raw.get("erase", False)),
    }


def _sanitize_path_point(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    out = {
        "x": _clamp(_finite(raw.get("x")), -10, 110),
        "y": _clamp(_finite(raw.get("y")), -10, 110),
    }
    for key in ("inX", "inY", "outX", "outY"):
        if raw.get(key) is not None:
            out[key] = _clamp(_finite(raw.get(key)), -1000, 1000)
    return out


def sanitize_mask(raw: Any) -> dict[str, Any] | None:
    """Clamps/validates a single mask dict; returns None if it is unusable."""
    if not isinstance(raw, dict):
        return None
    shape = raw.get("shape") if raw.get("shape") in _VALID_SHAPES else "rectangle"
    op = raw.get("op") if raw.get("op") in _VALID_OPS else "add"

    out: dict[str, Any] = {
        "id": str(raw.get("id") or "")[:200],
        "shape": shape,
        "enabled": bool(raw.get("enabled", True)),
        "op": op,
        "invert": bool(raw.get("invert", False)),
        "x": _clamp(_finite(raw.get("x")), -1000, 1000),
        "y": _clamp(_finite(raw.get("y")), -1000, 1000),
        "width": _clamp(_finite(raw.get("width")), 0, 1000),
        "height": _clamp(_finite(raw.get("height")), 0, 1000),
        "rotation": _finite(raw.get("rotation")) % 360,
        "feather": _clamp(_finite(raw.get("feather")), 0, 100),
        "roundness": _clamp(_finite(raw.get("roundness")), 0, 100),
    }

    stripes = raw.get("stripes")
    if stripes is not None:
        out["stripes"] = int(_clamp(_finite(stripes, 3), 2, 12))

    keyframes = raw.get("keyframes")
    if isinstance(keyframes, list) and keyframes:
        cleaned = [k for k in (_sanitize_keyframe(k) for k in keyframes[:MAX_KEYFRAMES]) if k is not None]
        cleaned.sort(key=lambda k: k["t"])
        if cleaned:
            out["keyframes"] = cleaned

    if shape == "brush":
        strokes = raw.get("strokes")
        cleaned_strokes = []
        if isinstance(strokes, list):
            for s in strokes[:MAX_STROKES_PER_MASK]:
                cs = _sanitize_stroke(s)
                if cs is not None:
                    cleaned_strokes.append(cs)
        out["strokes"] = cleaned_strokes

    if shape == "pen":
        path = raw.get("path")
        if isinstance(path, dict):
            raw_points = path.get("points")
            clean_points = []
            if isinstance(raw_points, list):
                for p in raw_points[:MAX_PATH_POINTS]:
                    cp = _sanitize_path_point(p)
                    if cp is not None:
                        clean_points.append(cp)
            out["path"] = {"points": clean_points, "closed": bool(path.get("closed", False))}

    return out


def sanitize_masks(raw: Any) -> list[dict[str, Any]]:
    """Validates/clamps an untrusted `masks` payload down to a safe list.

    Caps at MAX_MASKS entries, drops anything that isn't a well-formed dict,
    and clamps every numeric field so a hostile or malformed payload (NaN
    widths, million-point brush strokes, arbitrary op strings, ...) can
    never reach Pillow or an ffmpeg subprocess unbounded.
    """
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:MAX_MASKS]:
        cleaned = sanitize_mask(item)
        if cleaned is not None:
            out.append(cleaned)
    return out


def _scale_points(points: list[tuple[float, float]], sx: float, sy: float) -> list[tuple[float, float]]:
    return [(x * sx, y * sy) for x, y in points]


def render_matte_frame(masks: list[dict[str, Any]], t: float, size: tuple[int, int]) -> Image.Image:
    """Renders a single grayscale matte frame at time `t` for output `size`.

    White = keep, black = hide. If every mask is inert (disabled, degenerate,
    or an empty payload) the result is fully white — "no masking" — never a
    black frame; a black start would blank the whole video for every export
    where masking is simply absent.
    """
    masks = sanitize_masks(masks)
    width, height = size
    frame_aspect = width / height if height else 1.0

    if not masks or all(mask_is_inert(m) for m in masks):
        return Image.new("L", size, 255)

    sx = width / VIEWBOX
    sy = height / VIEWBOX

    matte = Image.new("L", size, 0)

    for mask in masks:
        polys = mask_polygons(mask, t, frame_aspect)
        if not polys:
            continue

        layer = Image.new("L", size, 0)
        draw = ImageDraw.Draw(layer)
        any_erase = False
        for poly in polys:
            pts = _scale_points(poly.points, sx, sy)
            if len(pts) < 2:
                continue
            if poly.stroke_width > 0:
                stroke_w = max(1, round(poly.stroke_width * ((sx + sy) / 2)))
                draw.line(pts, fill=255, width=stroke_w, joint="curve")
                # Round the caps so strokes don't look chopped at the ends.
                r = stroke_w / 2
                for px, py in (pts[0], pts[-1]):
                    draw.ellipse([px - r, py - r, px + r, py + r], fill=255)
            elif len(pts) >= 3:
                draw.polygon(pts, fill=255)
            if poly.erase:
                any_erase = True

        feather = float(mask.get("feather") or 0)
        if feather > 0:
            radius = feather * ((sx + sy) / 2) * (VIEWBOX / 1000) / 10
            if radius > 0:
                layer = layer.filter(ImageFilter.GaussianBlur(radius=radius))

        if mask.get("invert"):
            layer = ImageChops.invert(layer)

        if any_erase and mask.get("shape") == "brush":
            # Eraser strokes always cut a hole, regardless of the mask's op.
            matte = ImageChops.subtract(matte, layer)
            continue

        op = mask.get("op", "add")
        if op == "subtract":
            matte = ImageChops.subtract(matte, layer)
        elif op == "intersect":
            matte = ImageChops.darker(matte, layer)
        else:  # "add"
            matte = ImageChops.lighter(matte, layer)

    return matte


def render_matte_video(
    masks: list[dict[str, Any]],
    duration: float,
    fps: float,
    size: tuple[int, int],
    out_path: Path,
) -> Path | None:
    """Renders a grayscale FFV1/MKV matte video for a keep-range segment.

    Returns None (no ffmpeg invocation at all) when every mask is inert, so
    callers can skip the matte leg of the export entirely rather than
    generating a no-op all-white video.
    """
    clean = sanitize_masks(masks)
    if not clean or all(mask_is_inert(m) for m in clean):
        return None

    fps = max(1.0, float(fps) if fps and math.isfinite(fps) else 30.0)
    duration = max(0.04, float(duration) if duration and math.isfinite(duration) else 0.04)
    width, height = size
    frame_count = max(1, round(duration * fps))

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "-s",
        f"{width}x{height}",
        "-r",
        str(fps),
        "-i",
        "-",
        "-c:v",
        "ffv1",
        "-pix_fmt",
        "gray",
        str(out_path),
    ]

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert proc.stdin is not None
        for i in range(frame_count):
            t = i / fps
            frame = render_matte_frame(clean, t, size)
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
        _, stderr = proc.communicate(timeout=7200)
        if proc.returncode != 0:
            raise RuntimeError((stderr or b"").decode("utf-8", "replace")[-4000:])
    except Exception:
        proc.kill()
        raise

    return out_path
