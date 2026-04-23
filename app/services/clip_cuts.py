"""
Helpers for the multi-range `clips.cuts` field.

A cut list is an ordered list of kept ranges in source-video time:
    [{"start": 12.4, "end": 22.1}, {"start": 26.0, "end": 31.7}]

Invariants we enforce in `normalize_cuts`:
- Each range has strictly `end > start`.
- Ranges are sorted and non-overlapping (overlaps/touches are merged).
- Never empty: callers that need a degenerate "empty clip" should handle it
  explicitly; deletions that would remove everything collapse to a single
  minimal range so `Clip.start_time`/`end_time` stay coherent.

These helpers are pure so they can also back the rendering pipeline and the
frontend share the same semantics.
"""

from __future__ import annotations

from typing import Any, Iterable

MIN_RANGE = 0.05  # seconds; avoids zero-length cuts that break ffmpeg


def _coerce_range(raw: Any) -> tuple[float, float] | None:
    if not isinstance(raw, dict):
        return None
    try:
        s = float(raw.get("start"))
        e = float(raw.get("end"))
    except (TypeError, ValueError):
        return None
    if not (e > s):
        return None
    return (s, e)


def normalize_cuts(
    cuts: Iterable[Any] | None,
    *,
    fallback_start: float | None = None,
    fallback_end: float | None = None,
) -> list[dict[str, float]]:
    """Return a sorted, merged, validated cut list.

    If the input is empty/invalid and fallback bounds are supplied, returns a
    single-range list covering [fallback_start, fallback_end].
    """
    parsed: list[tuple[float, float]] = []
    for raw in cuts or []:
        pair = _coerce_range(raw)
        if pair is not None:
            parsed.append(pair)
    parsed.sort(key=lambda p: p[0])

    merged: list[list[float]] = []
    for s, e in parsed:
        if merged and s <= merged[-1][1] + 1e-4:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])

    out = [{"start": float(s), "end": float(e)} for s, e in merged if e - s >= MIN_RANGE / 2]

    if not out and fallback_start is not None and fallback_end is not None and fallback_end > fallback_start:
        out = [{"start": float(fallback_start), "end": float(fallback_end)}]
    return out


def cuts_bounds(cuts: list[dict[str, float]]) -> tuple[float, float]:
    """Return (min_start, max_end) for a non-empty cut list."""
    if not cuts:
        return (0.0, 0.0)
    return (float(cuts[0]["start"]), float(cuts[-1]["end"]))


def cuts_total_duration(cuts: list[dict[str, float]]) -> float:
    return sum(float(c["end"]) - float(c["start"]) for c in cuts)


def remove_interval(
    cuts: list[dict[str, float]],
    interval_start: float,
    interval_end: float,
) -> list[dict[str, float]]:
    """Subtract [interval_start, interval_end] from each kept range.

    Splits ranges that contain the interval and shrinks overlapping ends.
    If the operation would remove every kept range, collapses to a single
    minimal range centered on the original first range's start, so clip
    bounds remain valid (users can undo or re-edit).
    """
    if interval_end <= interval_start or not cuts:
        return list(cuts)

    out: list[dict[str, float]] = []
    for r in cuts:
        rs = float(r["start"])
        re = float(r["end"])
        if interval_end <= rs or interval_start >= re:
            out.append({"start": rs, "end": re})
            continue
        if interval_start > rs:
            out.append({"start": rs, "end": min(re, interval_start)})
        if interval_end < re:
            out.append({"start": max(rs, interval_end), "end": re})

    out = [c for c in out if c["end"] - c["start"] >= MIN_RANGE]
    if not out:
        rs = float(cuts[0]["start"])
        out = [{"start": rs, "end": rs + MIN_RANGE}]
    return out
