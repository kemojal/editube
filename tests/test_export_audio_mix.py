"""The export mix's new audio features: crossfades, ducking, loudness."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.jobs.rough_cut_export import (
    _apply_loudness_target,
    _crossfade_boundaries,
    _crossfade_wav_command,
    _measure_loudness,
    _normalize_loudness_command,
    _timeline_audio_graph,
    _transition_audio_settings,
)


def _between(left: int, right: int, duration: float) -> dict[str, object]:
    return {
        "placement": "between",
        "leftIndex": left,
        "rightIndex": right,
        "duration": duration,
    }


class TestCrossfadeBoundaries:
    def test_only_adjacent_between_pairs_qualify(self):
        transitions = [
            _between(0, 1, 0.6),
            _between(1, 3, 0.6),  # non-adjacent → stays a hard cut
            {"placement": "in", "rightIndex": 0, "duration": 0.5},
        ]
        assert _crossfade_boundaries(transitions, 4) == {0: 0.6}

    def test_durations_cap_at_five_seconds(self):
        assert _crossfade_boundaries([_between(0, 1, 9.0)], 2) == {0: 5.0}


class TestCrossfadeCommand:
    def test_no_boundaries_means_no_command(self, tmp_path: Path):
        parts = [tmp_path / "a.wav", tmp_path / "b.wav"]
        assert _crossfade_wav_command(parts, {}, tmp_path / "o.wav") is None

    def test_graph_pads_both_sides_and_crossfades_the_boundary(self, tmp_path: Path):
        parts = [tmp_path / "a.wav", tmp_path / "b.wav", tmp_path / "c.wav"]
        command = _crossfade_wav_command(parts, {0: 0.6}, tmp_path / "o.wav")
        graph = command[command.index("-filter_complex") + 1]
        # Left of the crossfade pads its tail; right leads in with silence.
        assert "apad=pad_dur=0.300" in graph
        assert "adelay=delays=300:all=1" in graph
        assert "acrossfade=d=0.600:c1=tri:c2=tri" in graph
        # The un-transitioned boundary stays a plain concat.
        assert "concat=n=2:v=0:a=1" in graph
        assert command[command.index("-map") + 1] == "[m2]"


class TestFoldBetween:
    def test_between_halves_skip_when_crossfading(self):
        transitions = [_between(0, 1, 0.8)]
        folded = _transition_audio_settings(None, 0, transitions, fold_between=True)
        assert folded == {"fadeOut": 0.4}
        assert _transition_audio_settings(None, 0, transitions, fold_between=False) is None

    def test_edge_ramps_always_fold(self):
        transitions = [{"placement": "in", "rightIndex": 0, "duration": 0.5}]
        result = _transition_audio_settings(None, 0, transitions, fold_between=False)
        assert result == {"fadeIn": 0.5}


def _chunk(output_start: float = 1.0) -> dict[str, object]:
    return {
        "kind": "audio",
        "audioEnabled": True,
        "hasAudio": True,
        "outputStart": output_start,
        "outputEnd": output_start + 4.0,
        "settings": {},
    }


class TestDucking:
    def test_duck_emits_a_sidechain_keyed_by_the_base(self):
        graph, label = _timeline_audio_graph([_chunk()], base_has_audio=True, duck=True)
        joined = ";".join(graph)
        assert "sidechaincompress" in joined
        assert "asplit=2[duck_voice][duck_key]" in joined
        assert label == "[a]"

    def test_no_duck_without_the_flag(self):
        graph, _ = _timeline_audio_graph([_chunk()], base_has_audio=True, duck=False)
        assert "sidechaincompress" not in ";".join(graph)

    def test_no_duck_without_a_base_to_key_from(self):
        graph, _ = _timeline_audio_graph([_chunk()], base_has_audio=False, duck=True)
        assert "sidechaincompress" not in ";".join(graph)


MEASured = {
    "input_i": "-23.1",
    "input_tp": "-5.2",
    "input_lra": "6.0",
    "input_thresh": "-33.5",
    "target_offset": "0.3",
}


class TestLoudness:
    def test_video_pass_copies_the_picture(self, tmp_path: Path):
        command = _normalize_loudness_command(
            tmp_path / "in.mp4", tmp_path / "out.mp4", -14.0, MEASured, has_video=True
        )
        assert command[command.index("-c:v") + 1] == "copy"
        af = command[command.index("-af") + 1]
        assert "measured_I=-23.1" in af and "linear=true" in af and "I=-14.0" in af

    def test_wav_output_stays_pcm(self, tmp_path: Path):
        command = _normalize_loudness_command(
            tmp_path / "in.wav", tmp_path / "out.wav", -16.0, MEASured, has_video=False
        )
        assert "pcm_s16le" in command and "-c:v" not in command

    def test_measure_parses_the_json_block(self, monkeypatch, tmp_path: Path):
        stderr = (
            "frame=1 fps=0\n[Parsed_loudnorm_0 @ 0x0]\n"
            '{ "input_i" : "-23.1", "input_tp" : "-5.2", "input_lra" : "6.0",'
            ' "input_thresh" : "-33.5", "output_i" : "-14.0",'
            ' "target_offset" : "0.3" }\n'
        )
        monkeypatch.setattr(
            "app.jobs.rough_cut_export.subprocess.run",
            lambda *a, **k: SimpleNamespace(stderr=stderr, stdout="", returncode=0),
        )
        measured = _measure_loudness(tmp_path / "x.mp4", -14.0)
        assert measured == MEASured

    def test_unmeasurable_mix_ships_with_a_warning_not_a_failure(
        self, monkeypatch, tmp_path: Path
    ):
        monkeypatch.setattr(
            "app.jobs.rough_cut_export.subprocess.run",
            lambda *a, **k: SimpleNamespace(stderr="no json here", stdout="", returncode=0),
        )
        source = tmp_path / "in.mp4"
        result, warnings = _apply_loudness_target(
            source, tmp_path, "youtube", has_video=True, suffix="out.mp4"
        )
        assert result == source
        assert any("skipped" in w for w in warnings)

    def test_unknown_target_is_a_noop(self, tmp_path: Path):
        source = tmp_path / "in.mp4"
        result, warnings = _apply_loudness_target(
            source, tmp_path, "off", has_video=True, suffix="out.mp4"
        )
        assert result == source and warnings == []
