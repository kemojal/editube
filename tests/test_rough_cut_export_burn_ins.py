"""Client-rasterized overlay frames, on their way into the render.

Both helpers under test are pure and file-system-only -- no DB, no ffmpeg run
-- so they are cheap to hold to the contract the frontend writes against:
base64 PNG plus an output-timeline span, hostile input dropped one entry at a
time rather than failing a whole export.
"""

import base64
import struct
import zlib
from pathlib import Path

import pytest

from app.jobs.rough_cut_export import (
    MAX_BURN_INS,
    _burn_in_overlay_command,
    _sanitize_burn_ins,
)


def _png_bytes(width: int = 4, height: int = 4) -> bytes:
    """A real, minimal PNG -- the magic bytes alone are not what is checked."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + b"\x00\x00\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _png_b64() -> str:
    return base64.b64encode(_png_bytes()).decode("ascii")


def _entry(**patch):
    entry = {"png": _png_b64(), "start": 0.5, "end": 1.5}
    entry.update(patch)
    return entry


def test_accepts_a_well_formed_frame_and_writes_it(tmp_path: Path):
    entries, skipped = _sanitize_burn_ins([_entry()], tmp_path=tmp_path, output_duration=10.0)
    assert skipped == []
    assert len(entries) == 1
    written = Path(entries[0]["path"])
    assert written.exists()
    assert written.read_bytes().startswith(b"\x89PNG")


def test_tolerates_a_data_uri_prefix(tmp_path: Path):
    entries, skipped = _sanitize_burn_ins(
        [_entry(png=f"data:image/png;base64,{_png_b64()}")],
        tmp_path=tmp_path,
        output_duration=10.0,
    )
    assert skipped == []
    assert len(entries) == 1


def test_clamps_a_span_that_runs_past_the_render(tmp_path: Path):
    entries, _ = _sanitize_burn_ins(
        [_entry(start=0.2, end=99.0)], tmp_path=tmp_path, output_duration=2.0
    )
    assert entries[0]["end"] == pytest.approx(2.0)


def test_clamps_a_fade_to_half_the_span(tmp_path: Path):
    entries, _ = _sanitize_burn_ins(
        [_entry(start=0.0, end=1.0, fadeIn=99.0)], tmp_path=tmp_path, output_duration=10.0
    )
    assert entries[0]["fadeIn"] <= 0.5


@pytest.mark.parametrize(
    "patch",
    [
        {"png": base64.b64encode(b"not a png at all").decode("ascii")},
        {"png": "!!!! not base64 !!!!"},
        {"start": 3.0, "end": 1.0},
        {"start": float("inf")},
        {"png": ""},
    ],
    ids=["not-png", "bad-base64", "inverted-span", "non-finite", "empty"],
)
def test_drops_hostile_entries_without_failing_the_export(tmp_path: Path, patch):
    entries, skipped = _sanitize_burn_ins(
        [_entry(**patch)], tmp_path=tmp_path, output_duration=10.0
    )
    assert entries == []
    assert len(skipped) == 1


def test_one_bad_frame_does_not_cost_the_good_ones(tmp_path: Path):
    entries, skipped = _sanitize_burn_ins(
        [_entry(), _entry(png="!!!"), _entry(start=2.0, end=3.0)],
        tmp_path=tmp_path,
        output_duration=10.0,
    )
    assert len(entries) == 2
    assert len(skipped) == 1


def test_caps_the_number_of_frames(tmp_path: Path):
    entries, skipped = _sanitize_burn_ins(
        [_entry() for _ in range(MAX_BURN_INS + 5)], tmp_path=tmp_path, output_duration=10.0
    )
    assert len(entries) == MAX_BURN_INS
    assert any("limit" in reason for reason in skipped)


def test_no_output_duration_means_nothing_can_be_placed(tmp_path: Path):
    entries, skipped = _sanitize_burn_ins([_entry()], tmp_path=tmp_path, output_duration=0.0)
    assert entries == []
    assert skipped


def test_empty_input_is_a_no_op(tmp_path: Path):
    assert _sanitize_burn_ins([], tmp_path=tmp_path, output_duration=10.0) == ([], [])
    assert _sanitize_burn_ins(None, tmp_path=tmp_path, output_duration=10.0) == ([], [])


def test_command_places_every_frame_and_copies_the_audio(tmp_path: Path):
    entries, _ = _sanitize_burn_ins(
        [_entry(start=0.2, end=1.2, fadeIn=0.3), _entry(start=1.0, end=2.0)],
        tmp_path=tmp_path,
        output_duration=5.0,
    )
    command = _burn_in_overlay_command(
        base_video=tmp_path / "in.mp4",
        burn_ins=entries,
        width=320,
        height=240,
        frame_rate=30.0,
        crf=23,
        output=tmp_path / "out.mp4",
    )

    joined = " ".join(command)
    # Every frame is an input, and every frame gets a gated overlay.
    assert joined.count("-loop 1") == 2
    assert joined.count("overlay=0:0") == 2
    assert "between(t,0.200000,1.200000)" in joined
    assert "between(t,1.000000,2.000000)" in joined
    # A requested fade ramps alpha; an unrequested one adds no filter.
    assert joined.count("fade=t=in") == 1
    # The audio is never re-encoded by this pass.
    assert "-c:a" in command and command[command.index("-c:a") + 1] == "copy"
    assert str(tmp_path / "out.mp4") == command[-1]


def test_command_scales_frames_to_the_real_video_size(tmp_path: Path):
    entries, _ = _sanitize_burn_ins([_entry()], tmp_path=tmp_path, output_duration=5.0)
    command = " ".join(
        _burn_in_overlay_command(
            base_video=tmp_path / "in.mp4",
            burn_ins=entries,
            width=1920,
            height=1080,
            frame_rate=30.0,
            crf=23,
            output=tmp_path / "out.mp4",
        )
    )
    assert "scale=1920:1080" in command
