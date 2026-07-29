"""Python mirror of the browser's mask geometry
(`editube-frontend/app/(sites)/dashboard/rough-cut/_lib/mask/mask-geometry.ts`
and `mask-keyframes.ts`).

The browser preview renders masks as SVG paths; Pillow (used by the server-side
MP4 exporter) draws polygons. This module therefore mirrors the same math but
emits flattened `MaskPolygon` objects instead of SVG path strings. Parity with
the TypeScript implementation is enforced by `tests/test_mask_geometry.py`
against the shared golden fixture
`editube-frontend/docs/fixtures/mask-geometry-golden.json` — see that test
file's docstring for exactly what parity tier each shape achieves.

Two rules that were previously introduced as bugs and must not return:
1. `_resolve_box`'s `shorter_axis_ratio` is `1 / frame_aspect` UNCONDITIONALLY
   for round shapes (circle/star/heart). There is no orientation ternary here
   — one was added once and it squashed round masks on portrait frames.
2. `sample_mask_transform` clamps at both ends of the keyframe range. Before
   the first keyframe you get the first keyframe's values; after the last,
   the last's. It never extrapolates.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# Every polygon is emitted in this square space; the consumer scales it onto
# the actual frame.
VIEWBOX = 1000

# Shapes that stay round: they size against the frame's shorter axis so an
# aspect change never turns a circle into an ellipse.
ROUND_SHAPES = {"circle", "star", "heart"}

# Expansion -> feMorphology radius contract. MUST stay in sync with
# `EXPANSION_RADIUS_FACTOR`/`expansionRadius` in the TypeScript mirror
# (mask-geometry.ts) or the exported MP4 dilates/erodes by a different
# amount than what the editor previewed.
#
# The Expansion slider runs -100..100 (0 = no change), mapped to a
# VIEWBOX-space (0-1000) radius using the same "% of the shape's shorter
# axis" convention as Feather:
#
#   radius = (|expansion| / 100) * min(width, height) * (VIEWBOX / 100) * EXPANSION_RADIUS_FACTOR
#
# On the TypeScript side that `radius` goes straight into `<feMorphology
# radius>`, which accepts a continuous float. Pillow has no continuous-radius
# morphology: `ImageFilter.MaxFilter`/`MinFilter` take an odd integer
# **kernel size**, and the standard radius<->kernel identity is
# `kernel = 2r + 1`. `mask_matte.py` rounds `r` (converted to output pixels)
# to the nearest integer before building that kernel, since there is no way
# to represent a fractional radius in Pillow's morphology filters. That
# rounding is the ONLY drift between the two renderers: a fixed, bounded
# (<= 0.5px) offset that does not compound across frames and is well under
# one visible pixel at any real export resolution.
EXPANSION_RADIUS_FACTOR = 0.25


def expansion_radius(mask: dict[str, Any]) -> float:
    """Mirrors `expansionRadius` in mask-geometry.ts — see the contract
    comment on `EXPANSION_RADIUS_FACTOR` above."""
    width = float(mask.get("width") or 0)
    height = float(mask.get("height") or 0)
    shorter = min(width, height)
    expansion = float(mask.get("expansion") or 0)
    return (abs(expansion) / 100.0) * shorter * (VIEWBOX / 100.0) * EXPANSION_RADIUS_FACTOR

_CIRCLE_SEGMENTS = 64
_CUBIC_SEGMENTS = 24
_CORNER_ARC_SEGMENTS = 8


@dataclass(frozen=True)
class MaskTransform:
    x: float
    y: float
    width: float
    height: float
    rotation: float


@dataclass(frozen=True)
class MaskPolygon:
    points: list[tuple[float, float]]
    """VIEWBOX-space coordinates."""
    stroke_width: float = 0.0
    """0 means fill the polygon; > 0 means stroke the polyline at this width."""
    erase: bool = False
    """Brush eraser strokes paint black regardless of the mask's op."""


@dataclass(frozen=True)
class _Box:
    centre_x: float
    centre_y: float
    width: float
    height: float
    left: float
    top: float


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _js_mod(a: float, b: float) -> float:
    """Mirrors JavaScript's `%` (truncated, sign-of-dividend), not Python's."""
    return math.fmod(a, b)


def _lerp_angle(a: float, b: float, t: float) -> float:
    """Interpolates rotation along the shorter arc, e.g. 350 -> 10 travels +20."""
    delta = _js_mod(b - a, 360)
    if delta > 180:
        delta -= 360
    if delta < -180:
        delta += 360
    return a + delta * t


# Channels with no base property on the mask dict yet (zoom lands in a later
# task) fall back to these fixed defaults, mirroring
# `CHANNEL_FALLBACK_DEFAULT` in mask-keyframes.ts. `expansion` DOES have a
# base property now (Task 2) — `_base_channel_value`'s `channel in mask`
# check picks it up like `feather`/`roundness` without needing a case here.
_CHANNEL_FALLBACK_DEFAULT: dict[str, float] = {"zoom": 100.0}

_TRANSFORM_CHANNELS = ("x", "y", "width", "height", "rotation")


def _base_channel_value(mask: dict[str, Any], channel: str) -> float:
    if channel in mask:
        return mask[channel]
    return _CHANNEL_FALLBACK_DEFAULT.get(channel, 0.0)


def sample_mask_channel(mask: dict[str, Any], channel: str, t: float) -> float:
    """Samples one channel of the per-property keyframe map (`mask["keyframes"]
    == {channel: [{"t": ..., "v": ...}, ...]}`), mirroring
    `sampleMaskChannel` in mask-keyframes.ts: clamp at both ends, never
    extrapolate; a channel with no keyframes falls back to the mask's base
    value for that property.
    """
    keyframes = (mask.get("keyframes") or {}).get(channel)
    if not keyframes:
        return _base_channel_value(mask, channel)

    if len(keyframes) == 1 or t <= keyframes[0]["t"]:
        return keyframes[0]["v"]

    last = keyframes[-1]
    if t >= last["t"]:
        return last["v"]

    index = 0
    while index < len(keyframes) - 2 and keyframes[index + 1]["t"] <= t:
        index += 1
    frm = keyframes[index]
    to = keyframes[index + 1]
    span = to["t"] - frm["t"]
    ratio = 0 if span <= 0 else (t - frm["t"]) / span

    if channel == "rotation":
        return _lerp_angle(frm["v"], to["v"], ratio)
    return _lerp(frm["v"], to["v"], ratio)


def sample_mask_transform(mask: dict[str, Any], t: float) -> MaskTransform:
    """Assembles the whole transform by sampling each of the five transform
    channels independently — mirrors `sampleMaskTransform` in
    mask-keyframes.ts. `mask["keyframes"]` is the per-channel map; a mask
    with no keyframes at all (or `None`) samples every channel to its base
    value.
    """
    return MaskTransform(
        x=sample_mask_channel(mask, "x", t),
        y=sample_mask_channel(mask, "y", t),
        width=sample_mask_channel(mask, "width", t),
        height=sample_mask_channel(mask, "height", t),
        rotation=sample_mask_channel(mask, "rotation", t),
    )


def _resolve_box(mask: dict[str, Any], transform: MaskTransform, frame_aspect: float) -> _Box:
    # No orientation branch here — see module docstring rule 1.
    shorter_axis_ratio = 1 / frame_aspect
    round_shape = mask["shape"] in ROUND_SHAPES
    width = (
        min(transform.width, transform.height) * shorter_axis_ratio if round_shape else transform.width
    ) / 100 * VIEWBOX
    height = (
        min(transform.width, transform.height) if round_shape else transform.height
    ) / 100 * VIEWBOX
    centre_x = VIEWBOX / 2 + (transform.x / 100) * VIEWBOX
    centre_y = VIEWBOX / 2 + (transform.y / 100) * VIEWBOX
    return _Box(
        centre_x=centre_x,
        centre_y=centre_y,
        width=width,
        height=height,
        left=centre_x - width / 2,
        top=centre_y - height / 2,
    )


def maskIsInert(mask: dict[str, Any]) -> bool:  # noqa: N802 - kept for direct TS-name grep-ability
    return mask_is_inert(mask)


def mask_is_inert(mask: dict[str, Any]) -> bool:
    if not mask.get("enabled"):
        return True
    shape = mask["shape"]
    if shape == "brush":
        strokes = mask.get("strokes") or []
        return not any(len(stroke["points"]) >= 4 for stroke in strokes)
    if shape == "pen":
        path = mask.get("path")
        points = path["points"] if path else []
        return len(points) < 3
    if shape == "split":
        return False
    return mask["width"] <= 0 or mask["height"] <= 0


# --- Shape polygon builders -------------------------------------------------


def _rectangle_corners(box: _Box) -> list[tuple[float, float]]:
    left, top, width, height = box.left, box.top, box.width, box.height
    return [
        (left, top),
        (left + width, top),
        (left + width, top + height),
        (left, top + height),
    ]


def _corner_arc(
    centre: tuple[float, float], radius: float, start_angle: float, end_angle: float, segments: int
) -> list[tuple[float, float]]:
    points = []
    for i in range(1, segments + 1):
        angle = start_angle + (end_angle - start_angle) * (i / segments)
        points.append((centre[0] + math.cos(angle) * radius, centre[1] + math.sin(angle) * radius))
    return points


def _rounded_rectangle_points(box: _Box, roundness: float) -> list[tuple[float, float]]:
    radius = (min(box.width, box.height) / 2) * (roundness / 100)
    left, top, width, height = box.left, box.top, box.width, box.height
    if radius <= 0.01:
        return _rectangle_corners(box)

    # Same order as the TS path: start after the top-left corner, go
    # clockwise (SVG y-down): top edge, top-right arc, right edge,
    # bottom-right arc, bottom edge, bottom-left arc, left edge, top-left arc.
    points: list[tuple[float, float]] = [(left + radius, top)]
    points.append((left + width - radius, top))
    points.extend(
        _corner_arc((left + width - radius, top + radius), radius, -math.pi / 2, 0, _CORNER_ARC_SEGMENTS)
    )
    points.append((left + width, top + height - radius))
    points.extend(
        _corner_arc(
            (left + width - radius, top + height - radius), radius, 0, math.pi / 2, _CORNER_ARC_SEGMENTS
        )
    )
    points.append((left + radius, top + height))
    points.extend(
        _corner_arc(
            (left + radius, top + height - radius), radius, math.pi / 2, math.pi, _CORNER_ARC_SEGMENTS
        )
    )
    points.append((left, top + radius))
    points.extend(
        _corner_arc((left + radius, top + radius), radius, math.pi, 1.5 * math.pi, _CORNER_ARC_SEGMENTS)
    )
    # The last arc's final sample lands back on the start point (the path is
    # closed) — drop the duplicate so the polygon has exactly 4 straight
    # corners + 4 * _CORNER_ARC_SEGMENTS vertices, not one extra.
    if points and math.isclose(points[-1][0], points[0][0], abs_tol=1e-6) and math.isclose(
        points[-1][1], points[0][1], abs_tol=1e-6
    ):
        points.pop()
    return points


def _circle_points(box: _Box) -> list[tuple[float, float]]:
    rx = box.width / 2
    ry = box.height / 2
    points = []
    for i in range(_CIRCLE_SEGMENTS):
        angle = 2 * math.pi * (i / _CIRCLE_SEGMENTS)
        points.append((box.centre_x + math.cos(angle) * rx, box.centre_y + math.sin(angle) * ry))
    return points


def _split_points(box: _Box) -> list[tuple[float, float]]:
    reach = VIEWBOX * 2
    left = box.centre_x - reach
    right = box.centre_x + reach
    top = box.centre_y - reach
    bottom = box.centre_y
    return [(left, top), (right, top), (right, bottom), (left, bottom)]


def _filmstrip_points(box: _Box) -> list[tuple[float, float]]:
    """A single full-width horizontal band — CapCut's Filmstrip. `Size` is the
    band's height only; width always spans the frame, so (like
    `_split_points`) the horizontal reach is oversized well past the frame so
    the mask's own rotation can never pull a vertical edge into view.
    """
    reach = VIEWBOX * 2
    half_height = box.height / 2
    left = box.centre_x - reach
    right = box.centre_x + reach
    top = box.centre_y - half_height
    bottom = box.centre_y + half_height
    return [(left, top), (right, top), (right, bottom), (left, bottom)]


def _star_points(box: _Box, points: int = 5, inner_ratio: float = 0.42) -> list[tuple[float, float]]:
    outer_x = box.width / 2
    outer_y = box.height / 2
    result = []
    for index in range(points * 2):
        ratio = 1 if index % 2 == 0 else inner_ratio
        angle = (math.pi * index) / points - math.pi / 2
        x = box.centre_x + math.cos(angle) * outer_x * ratio
        y = box.centre_y + math.sin(angle) * outer_y * ratio
        result.append((x, y))
    return result


def _cubic_point(p0, p1, p2, p3, t: float) -> tuple[float, float]:
    mt = 1 - t
    a = mt * mt * mt
    b = 3 * mt * mt * t
    c = 3 * mt * t * t
    d = t * t * t
    return (
        a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
        a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1],
    )


def _flatten_cubic(p0, p1, p2, p3, segments: int, include_start: bool) -> list[tuple[float, float]]:
    result = []
    start = 0 if include_start else 1
    for i in range(start, segments + 1):
        result.append(_cubic_point(p0, p1, p2, p3, i / segments))
    return result


def _heart_points(box: _Box) -> list[tuple[float, float]]:
    w, h = box.width, box.height
    x, y = box.left, box.top

    def p(fx: float, fy: float) -> tuple[float, float]:
        return (x + fx * w, y + fy * h)

    anchor0 = p(0.5, 1)
    c1 = [p(0.5, 1), p(0, 0.62), p(0, 0.36)]
    c2 = [p(0, 0.12), p(0.32, 0.02), p(0.5, 0.24)]
    c3 = [p(0.68, 0.02), p(1, 0.12), p(1, 0.36)]
    c4 = [p(1, 0.62), p(0.5, 1), p(0.5, 1)]

    points = [anchor0]
    prev = anchor0
    for ctrl in (c1, c2, c3, c4):
        points.extend(_flatten_cubic(prev, ctrl[0], ctrl[1], ctrl[2], _CUBIC_SEGMENTS, include_start=False))
        prev = ctrl[2]
    return points


def _text_polygons(box: _Box, roundness: float) -> list[MaskPolygon]:
    stem_width = box.width * 0.26
    bar_height = box.height * 0.24
    bar_box = _Box(
        centre_x=box.centre_x,
        centre_y=box.top + bar_height / 2,
        width=box.width,
        height=bar_height,
        left=box.left,
        top=box.top,
    )
    stem_left = box.centre_x - stem_width / 2
    stem_top = box.top + bar_height
    stem_height = box.height - bar_height
    stem_box = _Box(
        centre_x=box.centre_x,
        centre_y=stem_top + stem_height / 2,
        width=stem_width,
        height=stem_height,
        left=stem_left,
        top=stem_top,
    )
    return [
        MaskPolygon(points=_rounded_rectangle_points(bar_box, roundness)),
        MaskPolygon(points=_rounded_rectangle_points(stem_box, roundness)),
    ]


def freehand_project(transform: MaskTransform):
    scale_x = transform.width / 100
    scale_y = transform.height / 100
    offset_x = (transform.x / 100) * VIEWBOX + (VIEWBOX * (1 - scale_x)) / 2
    offset_y = (transform.y / 100) * VIEWBOX + (VIEWBOX * (1 - scale_y)) / 2

    def project(x: float, y: float) -> tuple[float, float]:
        return (offset_x + x * VIEWBOX * scale_x, offset_y + y * VIEWBOX * scale_y)

    return project


def freehand_unproject(transform: MaskTransform):
    scale_x = transform.width / 100
    scale_y = transform.height / 100
    offset_x = (transform.x / 100) * VIEWBOX + (VIEWBOX * (1 - scale_x)) / 2
    offset_y = (transform.y / 100) * VIEWBOX + (VIEWBOX * (1 - scale_y)) / 2

    def unproject(px: float, py: float) -> tuple[float, float]:
        x = (px * VIEWBOX - offset_x) / (VIEWBOX * scale_x) if scale_x != 0 else 0
        y = (py * VIEWBOX - offset_y) / (VIEWBOX * scale_y) if scale_y != 0 else 0
        return (x, y)

    return unproject


def stroke_render_width(stroke: dict[str, Any], transform: MaskTransform) -> float:
    scale = min(transform.width, transform.height) / 100
    return max(1.0, (stroke["size"] / 100) * VIEWBOX * scale)


def _brush_polygons(mask: dict[str, Any], transform: MaskTransform) -> list[MaskPolygon]:
    project = freehand_project(transform)
    polygons = []
    for stroke in mask.get("strokes") or []:
        pts = stroke["points"]
        if len(pts) < 4:
            continue
        stroke_points = [project(pts[i], pts[i + 1]) for i in range(0, len(pts) - 1, 2)]
        polygons.append(
            MaskPolygon(
                points=stroke_points,
                stroke_width=stroke_render_width(stroke, transform),
                erase=bool(stroke.get("erase", False)),
            )
        )
    return polygons


def _pen_polygon(mask: dict[str, Any], transform: MaskTransform) -> MaskPolygon | None:
    path = mask.get("path")
    if not path or len(path["points"]) < 3:
        return None
    project = freehand_project(transform)
    points = path["points"]
    n = len(points)
    first = project(points[0]["x"], points[0]["y"])
    result = [first]

    last_index = n if path.get("closed") else n - 1
    for index in range(last_index):
        frm = points[index]
        to = points[(index + 1) % n]
        target = project(to["x"], to["y"])
        has_handles = (
            frm.get("outX") is not None
            or frm.get("outY") is not None
            or to.get("inX") is not None
            or to.get("inY") is not None
        )
        if not has_handles:
            result.append(target)
            continue
        control1 = project(frm["x"] + frm.get("outX", 0), frm["y"] + frm.get("outY", 0))
        control2 = project(to["x"] + to.get("inX", 0), to["y"] + to.get("inY", 0))
        prev = result[-1]
        result.extend(
            _flatten_cubic(prev, control1, control2, target, _CUBIC_SEGMENTS, include_start=False)
        )
    return MaskPolygon(points=result)


def _rotate_points(points: list[tuple[float, float]], angle_deg: float, centre: tuple[float, float]):
    if angle_deg % 360 == 0:
        return points
    angle = math.radians(angle_deg)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    cx, cy = centre
    rotated = []
    for x, y in points:
        dx, dy = x - cx, y - cy
        rotated.append((cx + dx * cos_a - dy * sin_a, cy + dx * sin_a + dy * cos_a))
    return rotated


def mask_polygons(mask: dict[str, Any], t: float, frame_aspect: float) -> list[MaskPolygon]:
    if mask_is_inert(mask):
        return []
    transform = sample_mask_transform(mask, t)
    box = _resolve_box(mask, transform, frame_aspect)
    shape = mask["shape"]
    centre = (box.centre_x, box.centre_y)

    if shape == "rectangle":
        polys = [MaskPolygon(points=_rounded_rectangle_points(box, mask.get("roundness", 0)))]
    elif shape == "circle":
        polys = [MaskPolygon(points=_circle_points(box))]
    elif shape == "split":
        polys = [MaskPolygon(points=_split_points(box))]
    elif shape == "filmstrip":
        polys = [MaskPolygon(points=_filmstrip_points(box))]
    elif shape == "star":
        polys = [MaskPolygon(points=_star_points(box))]
    elif shape == "heart":
        polys = [MaskPolygon(points=_heart_points(box))]
    elif shape == "text":
        polys = _text_polygons(box, mask.get("roundness", 0))
    elif shape == "brush":
        polys = _brush_polygons(mask, transform)
    elif shape == "pen":
        polygon = _pen_polygon(mask, transform)
        polys = [polygon] if polygon else []
    else:
        polys = []

    return [
        MaskPolygon(points=_rotate_points(poly.points, transform.rotation, centre), stroke_width=poly.stroke_width, erase=poly.erase)
        for poly in polys
    ]
