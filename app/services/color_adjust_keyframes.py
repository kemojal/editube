"""Safe, bounded FFmpeg color animation built from inspector keyframes."""

from __future__ import annotations

import copy
import itertools
import json
import math
from typing import Any

from app.services.color_adjust import build_adjust_filter_chain

_MAX_TRACKS = 64
_MAX_POINTS_PER_TRACK = 200
_FILTER_BUDGET = 80_000
_SETTING_KEYS = {
    "enabled", "preset", "temp", "tint", "saturation", "vibrance",
    "exposure", "contrast", "highlight", "shadow", "whites", "blacks",
    "brilliance", "fade", "clarity", "sharpen", "vignette", "grain", "hsl",
    "curves", "wheels", "lut",
}


def _track(value: Any, duration: float) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    points: list[dict[str, Any]] = []
    for raw in value[:_MAX_POINTS_PER_TRACK]:
        if not isinstance(raw, dict):
            continue
        try:
            at = float(raw.get("t", 0))
            number = float(raw.get("v", 0))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(at) or not math.isfinite(number):
            continue
        points.append({
            "t": max(0.0, min(duration, at)),
            "v": number,
            "easing": str(raw.get("easing") or "linear"),
        })
    points.sort(key=lambda item: item["t"])
    deduped: list[dict[str, Any]] = []
    for point in points:
        if deduped and abs(point["t"] - deduped[-1]["t"]) < 0.0005:
            deduped[-1] = point
        else:
            deduped.append(point)
    return deduped


# Same constants `rough_cut_export.py` uses for its ffmpeg-expression easing;
# `tests/fixtures/easing_curves.json` is the contract that keeps this file, the
# export expressions, and the editor's `easingRatio` in agreement.
_EASE_C1 = 1.70158
_EASE_C3 = _EASE_C1 + 1.0
_EASE_OVERSHOOT_C1 = _EASE_C1 * 1.525
_EASE_OVERSHOOT_C3 = _EASE_OVERSHOOT_C1 + 1.0


def _back_out(ratio: float, c1: float, c3: float) -> float:
    return 1 + c3 * (ratio - 1) ** 3 + c1 * (ratio - 1) ** 2


def _ease(ratio: float, easing: str) -> float:
    """One keyframe segment's easing.

    The full craft set, not just the quadratics: color keyframes sample through
    this, and a curve the geometry channels honour but the grade silently
    linearises would export motion and colour drifting apart on the same clip.
    """
    ratio = max(0.0, min(1.0, ratio))
    if easing == "hold":
        return 0.0
    if easing == "ease-in":
        return ratio * ratio
    if easing == "ease-out":
        return 1 - (1 - ratio) * (1 - ratio)
    if easing == "ease-in-out":
        return 2 * ratio * ratio if ratio < 0.5 else 1 - ((-2 * ratio + 2) ** 2) / 2
    if easing == "smooth":
        return 4 * ratio ** 3 if ratio < 0.5 else 1 - ((-2 * ratio + 2) ** 3) / 2
    if easing == "glide":
        return 1 - (1 - ratio) ** 3
    if easing == "snappy":
        return 1 - (1 - ratio) ** 5
    if easing == "anticipate":
        return _EASE_C3 * ratio ** 3 - _EASE_C1 * ratio ** 2
    if easing == "settle":
        return _back_out(ratio, _EASE_C1, _EASE_C3)
    if easing == "overshoot":
        return _back_out(ratio, _EASE_OVERSHOOT_C1, _EASE_OVERSHOOT_C3)
    return ratio


def _sample(points: list[dict[str, Any]], at: float) -> float:
    if at <= points[0]["t"]:
        return float(points[0]["v"])
    if at >= points[-1]["t"]:
        return float(points[-1]["v"])
    for start, end in zip(points, points[1:]):
        if at > end["t"]:
            continue
        span = max(0.0005, float(end["t"]) - float(start["t"]))
        ratio = _ease((at - float(start["t"])) / span, str(start["easing"]))
        return float(start["v"]) + (float(end["v"]) - float(start["v"])) * ratio
    return float(points[-1]["v"])


def _set_nested(target: dict[str, Any], path: list[str], value: float) -> None:
    cursor = target
    for key in path[:-1]:
        child = cursor.get(key)
        if not isinstance(child, dict):
            child = {}
            cursor[key] = child
        cursor = child
    cursor[path[-1]] = value


def _prepared_tracks(settings: dict[str, Any], duration: float) -> dict[str, list[dict[str, Any]]]:
    keyframes = settings.get("keyframes")
    if not isinstance(keyframes, dict):
        return {}
    tracks: dict[str, list[dict[str, Any]]] = {}
    for channel, raw in itertools.islice(keyframes.items(), _MAX_TRACKS):
        name = str(channel)
        if not name.startswith("adjust."):
            continue
        points = _track(raw, duration)
        if points:
            tracks[name.removeprefix("adjust.")] = points
    return tracks


def _sample_settings(settings: dict[str, Any], tracks: dict[str, list[dict[str, Any]]], at: float) -> dict[str, Any]:
    sampled = copy.deepcopy({key: value for key, value in settings.items() if key in _SETTING_KEYS})
    for path, points in tracks.items():
        _set_nested(sampled, path.split("."), _sample(points, at))
    return sampled


def _enabled_filter(filter_text: str, start: float, end: float, *, final: bool) -> str:
    operator = "lte" if final else "lt"
    # Commas belong to the enable expression, not the outer filter chain.
    enabled = f"gte(t\\,{start:.6f})*{operator}(t\\,{end:.6f})"
    return f"{filter_text}:enable='{enabled}'"


def build_keyframed_adjust_filter_chain(settings: dict[str, Any] | None, duration: float) -> list[str]:
    """Build the exact static color engine over bounded temporal slices.

    FFmpeg's color filters do not share one expression API. Sampling the same
    settings function used by the editor and enabling the normal filter chain
    per slice keeps HSL, wheels, curves, temperature, finishing, and mixed
    keyframes consistent instead of implementing only the easy sliders.
    """
    if not isinstance(settings, dict) or settings.get("enabled") is False:
        return []
    duration = max(0.04, min(24 * 60 * 60, float(duration)))
    tracks = _prepared_tracks(settings, duration)
    if not tracks:
        return build_adjust_filter_chain(settings)

    representative = build_adjust_filter_chain(_sample_settings(settings, tracks, duration / 2))
    representative_size = max(240, len(",".join(representative)) + len(representative) * 56)
    max_slices = max(2, min(240, _FILTER_BUDGET // representative_size))
    slice_count = max(2, min(max_slices, math.ceil(duration * 24)))

    while True:
        segments: list[dict[str, Any]] = []
        for index in range(slice_count):
            start = duration * index / slice_count
            end = duration * (index + 1) / slice_count
            filters = build_adjust_filter_chain(_sample_settings(settings, tracks, (start + end) / 2))
            signature = json.dumps(filters, separators=(",", ":"))
            if segments and segments[-1]["signature"] == signature:
                segments[-1]["end"] = end
            else:
                segments.append({"start": start, "end": end, "filters": filters, "signature": signature})

        if len(segments) == 1:
            return list(segments[0]["filters"])

        result: list[str] = []
        for index, segment in enumerate(segments):
            for filter_text in segment["filters"]:
                result.append(_enabled_filter(
                    filter_text,
                    float(segment["start"]),
                    float(segment["end"]),
                    final=index == len(segments) - 1,
                ))
        if sum(len(item) + 1 for item in result) <= _FILTER_BUDGET or slice_count <= 2:
            return result
        slice_count = max(2, slice_count // 2)
