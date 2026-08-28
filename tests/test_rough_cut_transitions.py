from pathlib import Path

import pytest

from app.jobs.rough_cut_export import (
    _normalize_transitions,
    _transition_audio_settings,
    _transition_video_command,
)


RANGES = [(0.0, 4.0), (8.0, 8.4), (12.0, 16.0)]


def test_normalize_transitions_resolves_ranges_clamps_and_rejects_orphans():
    transitions = _normalize_transitions(
        [
            {
                "id": "valid",
                "presetId": "cube",
                "exportPreset": "coverleft",
                "placement": "between",
                "leftRange": {"start": 0, "end": 4},
                "rightRange": {"start": 8, "end": 8.4},
                "duration": 4,
            },
            {
                "id": "orphan",
                "placement": "between",
                "leftRange": {"start": 0, "end": 4},
                "rightRange": {"start": 12, "end": 16},
            },
        ],
        RANGES,
    )
    assert len(transitions) == 1
    assert transitions[0]["preset"] == "coverleft"
    assert transitions[0]["duration"] == pytest.approx(0.8)


def test_unknown_export_preset_falls_back_to_dissolve():
    transition = _normalize_transitions(
        [{"placement": "in", "rightIndex": 0, "exportPreset": "shell;rm", "duration": 0.5}],
        RANGES,
    )[0]
    assert transition["preset"] == "dissolve"


def test_transition_graph_preserves_runtime_with_half_handles():
    transitions = _normalize_transitions(
        [{"placement": "between", "leftIndex": 0, "rightIndex": 1, "exportPreset": "wipeleft", "duration": 0.6}],
        RANGES,
    )
    command = _transition_video_command(
        [Path("a.mp4"), Path("b.mp4"), Path("c.mp4")],
        [4.0, 0.4, 4.0],
        transitions,
        audio_path=Path("audio.wav"),
        crf=23,
        output=Path("out.mp4"),
    )
    assert command is not None
    graph = command[command.index("-filter_complex") + 1]
    assert "tpad=stop_mode=clone:stop_duration=0.300000" in graph
    assert "tpad=start_mode=clone:start_duration=0.300000" in graph
    assert "xfade=transition=wipeleft:duration=0.600000:offset=3.700000" in graph
    assert "concat=n=2:v=1:a=0" in graph
    assert command[command.index("-map") + 1] == "[tvout]"


@pytest.mark.parametrize(
    ("alignment", "expected_tail", "expected_head", "expected_offset"),
    [
        ("start", "0.600000", None, "4.000000"),
        ("center", "0.300000", "0.300000", "3.700000"),
        ("end", None, "0.600000", "3.400000"),
    ],
)
def test_transition_graph_honours_alignment(
    alignment, expected_tail, expected_head, expected_offset
):
    transitions = _normalize_transitions(
        [{
            "placement": "between",
            "leftIndex": 0,
            "rightIndex": 1,
            "exportPreset": "dissolve",
            "duration": 0.6,
            "alignment": alignment,
        }],
        [(0.0, 4.0), (8.0, 12.0), (12.0, 16.0)],
    )
    command = _transition_video_command(
        [Path("a.mp4"), Path("b.mp4"), Path("c.mp4")],
        [4.0, 4.0, 4.0],
        transitions,
        audio_path=Path("audio.wav"),
        crf=23,
        output=Path("out.mp4"),
    )
    graph = command[command.index("-filter_complex") + 1]
    if expected_tail:
        assert f"tpad=stop_mode=clone:stop_duration={expected_tail}" in graph
    else:
        assert "[0:v]settb=AVTB,format=yuv420p,tpad=stop_mode" not in graph
    if expected_head:
        assert f"tpad=start_mode=clone:start_duration={expected_head}" in graph
    else:
        assert "[1:v]settb=AVTB,format=yuv420p,tpad=start_mode" not in graph
    assert f"duration=0.600000:offset={expected_offset}" in graph


def test_transition_audio_fades_compose_with_existing_gain_settings():
    transitions = _normalize_transitions(
        [{"placement": "between", "leftIndex": 0, "rightIndex": 1, "duration": 0.6}],
        RANGES,
    )
    left = _transition_audio_settings({"volume": -3, "fadeOut": 0.1}, 0, transitions)
    right = _transition_audio_settings(None, 1, transitions)
    assert left == {"volume": -3, "fadeOut": 0.3}
    assert right == {"fadeIn": 0.3}
