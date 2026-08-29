"""Text-overlay motion surviving the burn-in (plan Workstream E, item 9).

The client rasterizes each overlay with the viewer's own renderer -- the
pixels never drift -- and now also sends the overlay's animation settings so
the render can move the raster with the SAME curves the viewer plays
(`text-canvas-animation.ts`, reproduced expression for expression in
`_text_motion_channels`). These tests hold the three seams: hostile-input
sanitation, the expression math, and the assembled filter graph. A final
test runs real ffmpeg when one is available so the graph is known to parse.
"""

import base64
import shutil
import struct
import subprocess
import zlib
from pathlib import Path

import pytest

from app.jobs.rough_cut_export import (
    _burn_in_overlay_command,
    _sampled_motion_channels,
    _sanitize_burn_in_motion,
    _sanitize_burn_ins,
    _text_motion_channels,
)


def _png_bytes(width: int = 32, height: int = 32) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + b"\xff\x00\x00\xff" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _entry(**patch):
    entry = {
        "png": base64.b64encode(_png_bytes()).decode("ascii"),
        "start": 0.5,
        "end": 2.5,
    }
    entry.update(patch)
    return entry


MOTION = {
    "in": "pop",
    "out": "fade",
    "duration": 0.45,
    "em": 36.0,
    "box": {"x": 100.0, "y": 60.0, "width": 200.0, "height": 80.0},
    "frame": {"width": 640.0, "height": 360.0},
}


class TestSanitize:
    def test_a_full_block_passes_through(self):
        skipped: list[str] = []
        motion = _sanitize_burn_in_motion(dict(MOTION), 0, skipped)
        assert skipped == []
        assert motion == MOTION

    def test_no_presets_means_no_motion(self):
        assert _sanitize_burn_in_motion({"in": "none", "out": "none"}, 0, []) is None
        assert _sanitize_burn_in_motion({"in": "wobble"}, 0, []) is None
        assert _sanitize_burn_in_motion("nonsense", 0, []) is None

    def test_pivot_presets_degrade_to_fade_without_a_box(self):
        skipped: list[str] = []
        motion = _sanitize_burn_in_motion({"in": "pop", "out": "spin"}, 3, skipped)
        assert motion is not None
        assert motion["in"] == "fade" and motion["out"] == "fade"
        assert skipped == ["burnIn[3]:motionPivotMissing"]

    def test_translate_presets_survive_without_a_box(self):
        skipped: list[str] = []
        motion = _sanitize_burn_in_motion({"in": "rise", "out": "slide"}, 0, skipped)
        assert motion is not None
        assert motion["in"] == "rise" and motion["out"] == "slide"
        assert skipped == []

    def test_hostile_numbers_are_clamped_not_fatal(self):
        motion = _sanitize_burn_in_motion(
            {"in": "fade", "duration": 99.0, "em": -5.0, "box": {"x": float("nan")}},
            0,
            [],
        )
        assert motion is not None
        assert motion["duration"] == 3.0
        assert motion["em"] == 4.0
        assert motion["box"] is None

    def test_rides_through_the_entry_sanitizer(self, tmp_path: Path):
        entries, skipped = _sanitize_burn_ins(
            [_entry(motion=dict(MOTION)), _entry()],
            tmp_path=tmp_path,
            output_duration=10.0,
        )
        assert skipped == []
        assert entries[0]["motion"]["in"] == "pop"
        assert entries[1]["motion"] is None


class TestChannels:
    def test_fade_touches_only_opacity(self):
        channels = _text_motion_channels(
            {"in": "fade", "out": "none", "duration": 0.4, "em": 32.0}, 1.0, 5.0
        )
        assert channels["dx"] == "0" and channels["dy"] == "0"
        assert channels["scale"] == "1" and channels["rot"] == "0"
        assert "pow(1-" in channels["opacity"]

    def test_exit_is_the_outer_if_so_it_wins_on_short_spans(self):
        channels = _text_motion_channels(
            {"in": "fade", "out": "fade", "duration": 0.4, "em": 32.0}, 1.0, 5.0
        )
        # The viewer's `textAnimationState` checks the exit window first; the
        # compiled expression must nest the same way or a short overlay would
        # be entering and leaving at once.
        assert channels["opacity"].startswith("if(lte(5.000000-(t),0.400000)")

    def test_slide_and_rise_are_em_proportional(self):
        slide = _text_motion_channels(
            {"in": "slide", "out": "none", "duration": 0.4, "em": 40.0}, 0.0, 5.0
        )
        rise = _text_motion_channels(
            {"in": "none", "out": "rise", "duration": 0.4, "em": 40.0}, 0.0, 5.0
        )
        assert "*18.000000" in slide["dx"]  # 0.45em
        assert "*12.000000" in rise["dy"]  # exit rises away by 0.3em
        assert rise["dy"].count("-") >= 1  # ...upward

    def test_pop_overshoots_the_way_the_viewer_does(self):
        channels = _text_motion_channels(
            {"in": "pop", "out": "none", "duration": 0.4, "em": 32.0}, 0.0, 5.0
        )
        assert "0.82+" in channels["scale"] and "1.04-" in channels["scale"]


class TestCommand:
    def _command(self, entries, width=640, height=360):
        return _burn_in_overlay_command(
            base_video=Path("in.mp4"),
            burn_ins=entries,
            width=width,
            height=height,
            frame_rate=30.0,
            crf=23,
            output=Path("out.mp4"),
        )

    def test_animated_entry_crops_ramps_and_places_about_the_block(self, tmp_path: Path):
        entries, _ = _sanitize_burn_ins(
            [_entry(motion=dict(MOTION))], tmp_path=tmp_path, output_duration=10.0
        )
        joined = " ".join(self._command(entries))
        assert "crop=200:80:100:60" in joined
        # Alpha ramps in pixel time, about the block, gated to the span.
        assert "alpha(X,Y)*" in joined
        assert "eval=frame" in joined  # pop breathes via per-frame scale
        assert "200.0000-w/2" in joined and "100.0000-h/2" in joined
        assert "between(t,0.500000,2.500000)" in joined
        # Motion owns opacity -- the static fade filter must not double-dip.
        assert "fade=t=" not in joined

    def test_box_scales_when_the_client_rastered_at_another_size(self, tmp_path: Path):
        motion = dict(MOTION)
        entries, _ = _sanitize_burn_ins(
            [_entry(motion=motion)], tmp_path=tmp_path, output_duration=10.0
        )
        joined = " ".join(self._command(entries, width=1280, height=720))
        # 640x360 raster box doubled into the 1280x720 frame.
        assert "crop=400:160:200:120" in joined

    def test_spin_rotates_on_an_enlarged_transparent_canvas(self, tmp_path: Path):
        motion = {**MOTION, "in": "spin", "out": "none"}
        entries, _ = _sanitize_burn_ins(
            [_entry(motion=motion)], tmp_path=tmp_path, output_duration=10.0
        )
        joined = " ".join(self._command(entries))
        assert "rotate=angle=" in joined and ":c=none" in joined

    def test_static_entries_keep_the_exact_old_graph(self, tmp_path: Path):
        entries, _ = _sanitize_burn_ins(
            [_entry(fadeIn=0.3)], tmp_path=tmp_path, output_duration=10.0
        )
        joined = " ".join(self._command(entries))
        assert "overlay=0:0" in joined
        assert "fade=t=in:st=0.500000:d=0.300000:alpha=1" in joined
        assert "geq=" not in joined and "crop=" not in joined

    def test_translate_only_motion_skips_crop_and_scale(self, tmp_path: Path):
        motion = {"in": "rise", "out": "none", "duration": 0.4, "em": 30.0}
        entries, _ = _sanitize_burn_ins(
            [_entry(motion=motion)], tmp_path=tmp_path, output_duration=10.0
        )
        joined = " ".join(self._command(entries))
        assert "crop=" not in joined and "eval=frame" not in joined
        assert "rotate=" not in joined
        assert "-h/2+(if(" in joined  # the rise offset rides the overlay y


SAMPLED = {
    "box": MOTION["box"],
    "frame": MOTION["frame"],
    "phases": [
        {
            "at": "in",
            "duration": 0.5,
            "tracks": {
                "opacity": [{"t": 0.0, "v": 0.0}, {"t": 0.5, "v": 1.0}],
                "dy": [{"t": 0.0, "v": 40.0}, {"t": 0.25, "v": 8.0}, {"t": 0.5, "v": 0.0}],
                "scale": [{"t": 0.0, "v": 0.4}, {"t": 0.5, "v": 1.0}],
            },
        },
        {
            "at": "loop",
            "duration": 1.0,
            "tracks": {
                "rotation": [{"t": 0.0, "v": -4.0}, {"t": 0.5, "v": 4.0}, {"t": 1.0, "v": -4.0}],
            },
        },
    ],
}


class TestSampledForm:
    def test_sanitize_keeps_the_engine_samples(self):
        skipped: list[str] = []
        motion = _sanitize_burn_in_motion(dict(SAMPLED), 0, skipped)
        assert skipped == []
        assert motion is not None and len(motion["phases"]) == 2
        assert motion["phases"][0]["tracks"]["dy"][0] == {"t": 0.0, "v": 40.0}

    def test_pivot_tracks_are_dropped_without_a_box(self):
        skipped: list[str] = []
        motion = _sanitize_burn_in_motion(
            {"phases": SAMPLED["phases"]}, 2, skipped
        )
        assert motion is not None
        tracks = motion["phases"][0]["tracks"]
        assert "scale" not in tracks and "opacity" in tracks and "dy" in tracks
        # The loop phase held only rotation, so it fell away entirely.
        assert len(motion["phases"]) == 1
        assert skipped == ["burnIn[2]:motionPivotMissing"]

    def test_garbage_phases_yield_no_motion_with_a_reason(self):
        skipped: list[str] = []
        motion = _sanitize_burn_in_motion(
            {"phases": [{"at": "in", "duration": "soon", "tracks": {}}]}, 5, skipped
        )
        assert motion is None
        assert skipped == ["burnIn[5]:motionInvalid"]

    def test_hostile_samples_are_clamped(self):
        motion = _sanitize_burn_in_motion(
            {
                "box": MOTION["box"],
                "phases": [
                    {
                        "at": "in",
                        "duration": 0.5,
                        "tracks": {
                            "opacity": [{"t": -3.0, "v": 9.0}, {"t": 99.0, "v": -1.0}],
                        },
                    }
                ],
            },
            0,
            [],
        )
        samples = motion["phases"][0]["tracks"]["opacity"]
        assert samples[0] == {"t": 0.0, "v": 1.5}
        assert samples[1] == {"t": 0.5, "v": 0.0}

    def test_channels_merge_like_the_element_engine(self):
        channels = _sampled_motion_channels(SAMPLED, 1.0, 5.0)
        # The in-phase gates on its window; the loop wraps for the whole span.
        assert channels["opacity"].startswith("if(lte((t)-1.000000,0.500000)")
        assert "mod((t)-1.000000,1.000000)" in channels["rot"]
        # dy interpolates through the mid sample.
        assert "40.000000+(-32.000000)*" in channels["dy"]

    def test_an_out_phase_ends_at_the_exit(self):
        channels = _sampled_motion_channels(
            {
                "phases": [
                    {
                        "at": "out",
                        "duration": 0.4,
                        "tracks": {"opacity": [{"t": 0.0, "v": 1.0}, {"t": 0.4, "v": 0.0}]},
                    }
                ]
            },
            1.0,
            5.0,
        )
        assert "gte((t),4.600000)" in channels["opacity"]

    def test_command_animates_from_sampled_phases(self, tmp_path: Path):
        entries, skipped = _sanitize_burn_ins(
            [_entry(motion=dict(SAMPLED))], tmp_path=tmp_path, output_duration=10.0
        )
        assert skipped == []
        command = " ".join(
            _burn_in_overlay_command(
                base_video=Path("in.mp4"),
                burn_ins=entries,
                width=640,
                height=360,
                frame_rate=30.0,
                crf=23,
                output=Path("out.mp4"),
            )
        )
        assert "crop=200:80:100:60" in command
        assert "rotate=angle=" in command  # the loop's wobble
        assert "eval=frame" in command  # the sampled scale
        assert "alpha(X,Y)*" in command


class TestRevealTrack:
    REVEAL = {
        "box": MOTION["box"],
        "frame": MOTION["frame"],
        "phases": [
            {
                "at": "in",
                "duration": 0.42,
                "tracks": {
                    "reveal": [
                        {"t": 0.0, "v": 0.0},
                        {"t": 0.21, "v": 0.87},
                        {"t": 0.42, "v": 1.0},
                    ],
                },
            }
        ],
    }

    def test_sanitize_keeps_reveal_and_needs_no_pivot(self):
        skipped: list[str] = []
        motion = _sanitize_burn_in_motion(
            {"phases": self.REVEAL["phases"]}, 0, skipped
        )
        assert skipped == []
        assert motion["phases"][0]["tracks"]["reveal"][1] == {"t": 0.21, "v": 0.87}

    def test_the_wipe_is_an_alpha_gate_on_x(self, tmp_path: Path):
        entries, _ = _sanitize_burn_ins(
            [_entry(motion=dict(self.REVEAL))], tmp_path=tmp_path, output_duration=10.0
        )
        joined = " ".join(
            _burn_in_overlay_command(
                base_video=Path("in.mp4"),
                burn_ins=entries,
                width=640,
                height=360,
                frame_rate=30.0,
                crf=23,
                output=Path("out.mp4"),
            )
        )
        assert "gte(W*(" in joined
        # A pure reveal moves nothing: no rotate, no per-frame scale.
        assert "rotate=" not in joined and "eval=frame" not in joined
        # The box scopes the sweep to the block, not the frame.
        assert "crop=200:80:100:60" in joined

    @pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="no ffmpeg on this machine")
    def test_the_wipe_graph_actually_renders(self, tmp_path: Path):
        base = tmp_path / "base.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=640x360:d=2:r=30",
                "-pix_fmt", "yuv420p", str(base),
            ],
            check=True,
            capture_output=True,
        )
        entries, _ = _sanitize_burn_ins(
            [_entry(start=0.2, end=1.8, motion=dict(self.REVEAL))],
            tmp_path=tmp_path,
            output_duration=2.0,
        )
        output = tmp_path / "out.mp4"
        command = _burn_in_overlay_command(
            base_video=base, burn_ins=entries, width=640, height=360,
            frame_rate=30.0, crf=28, output=output,
        )
        result = subprocess.run(command, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr[-2000:]
        assert output.exists() and output.stat().st_size > 0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="no ffmpeg on this machine")
def test_the_sampled_graph_actually_renders(tmp_path: Path):
    base = tmp_path / "base.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=640x360:d=3:r=30",
            "-pix_fmt", "yuv420p", str(base),
        ],
        check=True,
        capture_output=True,
    )
    entries, _ = _sanitize_burn_ins(
        [_entry(motion=dict(SAMPLED))], tmp_path=tmp_path, output_duration=3.0
    )
    output = tmp_path / "out.mp4"
    command = _burn_in_overlay_command(
        base_video=base,
        burn_ins=entries,
        width=640,
        height=360,
        frame_rate=30.0,
        crf=28,
        output=output,
    )
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-2000:]
    assert output.exists() and output.stat().st_size > 0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="no ffmpeg on this machine")
def test_the_animated_graph_actually_renders(tmp_path: Path):
    base = tmp_path / "base.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=640x360:d=3:r=30",
            "-pix_fmt", "yuv420p", str(base),
        ],
        check=True,
        capture_output=True,
    )
    entries, skipped = _sanitize_burn_ins(
        [
            _entry(motion={**MOTION, "in": "pop", "out": "slide"}),
            _entry(start=1.0, end=2.0, fadeIn=0.2),
        ],
        tmp_path=tmp_path,
        output_duration=3.0,
    )
    assert skipped == []
    output = tmp_path / "out.mp4"
    command = _burn_in_overlay_command(
        base_video=base,
        burn_ins=entries,
        width=640,
        height=360,
        frame_rate=30.0,
        crf=28,
        output=output,
    )
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-2000:]
    assert output.exists() and output.stat().st_size > 0
