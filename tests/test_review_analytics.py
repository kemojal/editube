from dataclasses import dataclass

from app.services.review_analytics import (
    build_watch_heatmap,
    is_playback_complete,
    new_playback_milestones,
    normalize_progress_range,
)


@dataclass
class Event:
    session_id: int
    position: int
    range_end: int | None


def test_progress_ranges_are_clamped_to_video_duration():
    assert normalize_progress_range(-5, 120, 60) == (0, 60)
    assert normalize_progress_range(55, 500_000, 60) == (55, 60)
    assert normalize_progress_range(60, 61, 60) is None
    assert normalize_progress_range(20, 10, 60) is None


def test_heatmap_counts_unique_sessions_and_replays_separately():
    result = build_watch_heatmap(
        [
            Event(session_id=1, position=0, range_end=3),
            Event(session_id=1, position=1, range_end=3),
            Event(session_id=2, position=1, range_end=10),
        ],
        duration=3,
    )

    assert result.unique_views == {0: 1, 1: 2, 2: 2}
    assert result.replay_views == {1: 1, 2: 1}


def test_milestones_are_emitted_once_even_when_progress_repeats():
    assert new_playback_milestones(
        77,
        100,
        ended=False,
        already_reached=[25, 50],
    ) == (75,)
    assert new_playback_milestones(
        100,
        100,
        ended=True,
        already_reached=[25, 50, 75, 100],
    ) == ()


def test_completion_uses_ended_or_documented_95_percent_threshold():
    assert is_playback_complete(94, 100, ended=False) is False
    assert is_playback_complete(95, 100, ended=False) is True
    assert is_playback_complete(0, None, ended=True) is True
