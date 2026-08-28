"""Deterministic review playback analytics.

All interval math lives here so the write path and owner dashboard share the
same bounds and can be tested without a database or analytics provider.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import ceil
from typing import Iterable, Protocol


PLAYBACK_MILESTONES = (25, 50, 75, 100)
MAX_REVIEW_DURATION_SECONDS = 24 * 60 * 60


class ProgressEvent(Protocol):
    session_id: int
    position: int
    range_end: int | None


@dataclass(frozen=True)
class WatchHeatmap:
    unique_views: dict[int, int]
    replay_views: dict[int, int]


def bounded_duration(duration: int | float | None) -> int:
    if duration is None:
        return MAX_REVIEW_DURATION_SECONDS
    return max(0, min(int(ceil(float(duration))), MAX_REVIEW_DURATION_SECONDS))


def bounded_position(position: int | float | None, duration: int | float | None) -> int:
    limit = bounded_duration(duration)
    return max(0, min(int(position or 0), limit))


def normalize_progress_range(
    position: int | float | None,
    range_end: int | float | None,
    duration: int | float | None,
) -> tuple[int, int] | None:
    if range_end is None:
        return None
    start = bounded_position(position, duration)
    end = bounded_position(range_end, duration)
    if end <= start:
        return None
    return start, end


def build_watch_heatmap(
    events: Iterable[ProgressEvent],
    duration: int | float | None,
) -> WatchHeatmap:
    """Count unique sessions and true repeat traversals for each second."""

    sessions_by_second: dict[int, set[int]] = defaultdict(set)
    traversals_by_second: dict[int, int] = defaultdict(int)
    for event in events:
        interval = normalize_progress_range(event.position, event.range_end, duration)
        if interval is None:
            continue
        start, end = interval
        for second in range(start, end):
            sessions_by_second[second].add(int(event.session_id))
            traversals_by_second[second] += 1

    unique_views = {
        second: len(session_ids)
        for second, session_ids in sorted(sessions_by_second.items())
    }
    replay_views = {
        second: traversals_by_second[second] - unique_count
        for second, unique_count in unique_views.items()
        if traversals_by_second[second] > unique_count
    }
    return WatchHeatmap(unique_views=unique_views, replay_views=replay_views)


def playback_milestones(
    max_position: int | float | None,
    duration: int | float | None,
    *,
    ended: bool = False,
) -> tuple[int, ...]:
    actual_duration = bounded_duration(duration)
    if ended:
        return PLAYBACK_MILESTONES
    if duration is None or actual_duration <= 0:
        return ()
    position = bounded_position(max_position, actual_duration)
    return tuple(
        milestone
        for milestone in PLAYBACK_MILESTONES
        if position * 100 >= actual_duration * milestone
    )


def new_playback_milestones(
    max_position: int | float | None,
    duration: int | float | None,
    *,
    ended: bool,
    already_reached: Iterable[int],
) -> tuple[int, ...]:
    existing = {int(value) for value in already_reached}
    return tuple(
        value
        for value in playback_milestones(max_position, duration, ended=ended)
        if value not in existing
    )


def is_playback_complete(
    max_position: int | float | None,
    duration: int | float | None,
    *,
    ended: bool,
) -> bool:
    if ended:
        return True
    actual_duration = bounded_duration(duration)
    if duration is None or actual_duration <= 0:
        return False
    return bounded_position(max_position, actual_duration) * 100 >= actual_duration * 95
