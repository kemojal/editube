"""Translate editor marks into stable SAM prompt groups.

An include click is an independent request to add a region to the subject. It
must not be appended to the previous click as though both points describe one
connected object: doing that made a torso refinement erase an already-correct
head selection. Include regions are therefore segmented independently and
unioned, while every exclude mark is supplied as negative context to each
region.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


Point = tuple[float, float]


def _coordinate(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return max(0.0, min(1.0, number))


def _point(value: Any) -> Point | None:
    if not isinstance(value, dict):
        return None
    x = _coordinate(value.get("x"))
    y = _coordinate(value.get("y"))
    return (x, y) if x is not None and y is not None else None


@dataclass(frozen=True)
class SelectionPrompts:
    """Prompt groups with explicit additive and subtractive semantics."""

    positive_groups: tuple[tuple[Point, ...], ...]
    negative_points: tuple[Point, ...]

    @property
    def point_count(self) -> int:
        return sum(len(group) for group in self.positive_groups) + len(self.negative_points)

    def flattened(self) -> tuple[list[Point], list[int]]:
        """Compatibility form for APIs that still consume one point prompt."""
        positives = [point for group in self.positive_groups for point in group]
        return positives + list(self.negative_points), [1] * len(positives) + [
            0
        ] * len(self.negative_points)


def selection_prompts(settings: dict[str, Any]) -> SelectionPrompts | None:
    """Returns normalised editor prompts, or ``None`` without an include mark."""
    selection = settings.get("selection") or {}
    raw_points = selection.get("points") or []
    strokes = selection.get("strokes") or []

    positive_groups: list[tuple[Point, ...]] = []
    negative_points: list[Point] = []

    for raw in raw_points:
        point = _point(raw)
        if point is None:
            continue
        if raw.get("include", True):
            # Each click expands the current subject instead of redefining it.
            positive_groups.append((point,))
        else:
            negative_points.append(point)

    for stroke in strokes:
        if not isinstance(stroke, dict):
            continue
        vertices = stroke.get("points") or []
        step = max(1, len(vertices) // 12)
        sampled = tuple(
            point
            for point in (_point(vertex) for vertex in vertices[::step])
            if point is not None
        )
        if not sampled:
            continue
        if stroke.get("include", True):
            # One brush gesture describes one contiguous additive region.
            positive_groups.append(sampled)
        else:
            negative_points.extend(sampled)

    # Negative marks only refine an existing positive subject; they cannot
    # identify one on their own.
    if not positive_groups:
        return None
    return SelectionPrompts(tuple(positive_groups), tuple(negative_points))
