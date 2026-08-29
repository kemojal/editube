"""Styled caption export: CaptionStyle → ASS.

The last test burns the generated script through ffmpeg's subtitles filter —
the house rule from test_easing_parity: an expression (or here, a script) is
proven against the renderer that will consume it, not against our own parser.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.jobs.rough_cut_export import (
    _build_caption_burn_file,
    _remap_segments_with_words,
    _trim_worded_around_layers,
)
from app.services.rough_cut_captions import build_ass

STYLE = {
    "enabled": True,
    "fontFamily": "Montserrat",
    "fontWeight": 800,
    "italic": True,
    "fontSize": 5.0,
    "scale": 100,
    "letterSpacing": 0.05,
    "uppercase": True,
    "wordsPerLine": 2,
    "lines": 1,
    "position": "bottom",
    "textAlign": "center",
    "opacity": 100,
    "fill": "#FFFFFF",
    "activeFill": "#FFD400",
    "highlightStyle": "progressive",
    "stroke": True,
    "strokeColor": "#102030",
    "strokeWidth": 3,
    "backdrop": "none",
}

WORDS = [
    {"word": "big", "start": 1.0, "end": 1.4},
    {"word": "bold", "start": 1.5, "end": 1.9},
    {"word": "idea", "start": 2.0, "end": 2.6},
]

ENTRY = {"start": 1.0, "end": 2.6, "text": "big bold idea", "words": WORDS}


class TestBuildAss:
    def test_style_line_carries_the_typography(self):
        script, _ = build_ass([ENTRY], STYLE, play_res_x=1920, play_res_y=1080)
        style_line = next(line for line in script.splitlines() if line.startswith("Style:"))
        fields = style_line.split(",")
        assert fields[1] == "Montserrat"
        assert fields[2] == "54"  # 1080 * 5%
        assert fields[7] == "1" and fields[8] == "1"  # bold, italic
        assert float(fields[13]) == pytest.approx(0.05 * 54, abs=0.1)  # spacing
        assert fields[16] == "3"  # outline width
        assert fields[18] == "2"  # bottom-center alignment

    def test_colors_convert_to_ass_bgr_with_alpha(self):
        script, _ = build_ass([ENTRY], {**STYLE, "opacity": 50}, play_res_x=1920, play_res_y=1080)
        style_line = next(line for line in script.splitlines() if line.startswith("Style:"))
        # activeFill #FFD400 at 50% opacity → alpha 0x80, BGR 00D4FF.
        assert "&H8000D4FF" in style_line

    def test_progressive_highlight_emits_kf_karaoke_and_uppercases(self):
        script, _ = build_ass([ENTRY], STYLE, play_res_x=1920, play_res_y=1080)
        dialogue = [line for line in script.splitlines() if line.startswith("Dialogue:")]
        # wordsPerLine=2, lines=1 → two blocks: [big bold] [idea].
        assert len(dialogue) == 2
        assert r"{\kf50}BIG" in dialogue[0]  # runs to next word's start (0.5s)
        assert r"{\kf" in dialogue[1] and "IDEA" in dialogue[1]

    def test_no_highlight_renders_plain_lines_with_fill(self):
        script, _ = build_ass(
            [ENTRY], {**STYLE, "highlightStyle": "none", "uppercase": False},
            play_res_x=1920, play_res_y=1080,
        )
        dialogue = [line for line in script.splitlines() if line.startswith("Dialogue:")]
        assert len(dialogue) == 1
        assert "big bold idea" in dialogue[0]
        style_line = next(line for line in script.splitlines() if line.startswith("Style:"))
        assert style_line.split(",")[3] == "&H00FFFFFF"  # primary = fill

    def test_unmappable_features_warn_instead_of_vanishing(self):
        _, warnings = build_ass(
            [ENTRY],
            {**STYLE, "highlightStyle": "pill", "backdrop": "pill",
             "glowIntensity": 40, "rotation": 3, "keywordFill": "#FF0000"},
            play_res_x=1920, play_res_y=1080,
        )
        text = " ".join(warnings)
        assert "pill" in text and "backdrop" in text.lower()
        assert "glow" in text.lower()
        assert "rotation" in text.lower()
        assert "Keyword" in text

    def test_braces_are_escaped_so_text_cannot_inject_tags(self):
        script, _ = build_ass(
            [{"start": 0, "end": 1, "text": "hack {\\pos(0,0)} attempt", "words": []}],
            {**STYLE, "highlightStyle": "none", "uppercase": False},
            play_res_x=1920, play_res_y=1080,
        )
        assert r"\{" in script and r"{\pos" not in script


class TestWordRemap:
    def test_words_survive_the_cut_and_land_in_export_time(self):
        segments = [
            {"start": 0.0, "end": 10.0, "text": "one two three",
             "words": [
                 {"word": "one", "start": 1.0, "end": 1.5},
                 {"word": "two", "start": 5.0, "end": 5.5},   # cut out
                 {"word": "three", "start": 8.5, "end": 9.0},
             ]},
        ]
        entries = _remap_segments_with_words(segments, [(0.0, 2.0), (8.0, 10.0)])
        words = [w for entry in entries for w in entry["words"]]
        assert [w["word"] for w in words] == ["one", "three"]
        assert words[0]["start"] == pytest.approx(1.0)
        assert words[1]["start"] == pytest.approx(2.5)  # 2.0 + (8.5 - 8.0)

    def test_trimming_around_layer_captions_drops_their_words(self):
        worded = [{"start": 0.0, "end": 6.0, "text": "x", "words": [
            {"word": "early", "start": 0.05, "end": 0.15},
            {"word": "late", "start": 5.0, "end": 5.5},
        ]}]
        # Leading sliver (0.2s) is under the 0.25s floor and drops with its word.
        trimmed = _trim_worded_around_layers(worded, [(0.2, 4.5, "layer")])
        assert len(trimmed) == 1
        assert [w["word"] for w in trimmed[0]["words"]] == ["late"]
        assert trimmed[0]["start"] == pytest.approx(4.5)


class TestBurnFileSelection:
    def test_no_style_falls_back_to_srt(self, tmp_path: Path):
        path, renderer, warnings = _build_caption_burn_file(
            tmp_path,
            plain_entries=[(0.0, 1.0, "hi")],
            worded_entries=[],
            layer_entries=[],
            caption_style={},
            scale_w=1920,
            scale_h=1080,
        )
        assert renderer == "srt" and path.suffix == ".srt" and warnings == []
        assert "hi" in path.read_text()

    def test_style_builds_ass_and_reports_font_fallback(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("CAPTION_FONTS_DIR", raising=False)
        path, renderer, warnings = _build_caption_burn_file(
            tmp_path,
            plain_entries=[(1.0, 2.6, "big bold idea")],
            worded_entries=[ENTRY],
            layer_entries=[],
            caption_style=STYLE,
            scale_w=1920,
            scale_h=1080,
        )
        assert renderer == "ass" and path.suffix == ".ass"
        assert "[V4+ Styles]" in path.read_text()
        assert any("system" in w and "font" in w.lower() for w in warnings)


def _ffmpeg_has_subtitles_filter() -> bool:
    if shutil.which("ffmpeg") is None:
        return False
    try:
        listing = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:  # noqa: BLE001
        return False
    return any(line.split()[1:2] == ["subtitles"] for line in listing.splitlines())


@pytest.mark.skipif(
    not _ffmpeg_has_subtitles_filter(),
    reason="ffmpeg lacks the libass subtitles filter (a capability the export "
    "worker requires; probed by the harness capability registry)",
)
def test_generated_ass_burns_through_ffmpeg(tmp_path: Path):
    """libass must accept the script — proven by rendering it, not parsing it."""
    script, _ = build_ass([ENTRY], STYLE, play_res_x=320, play_res_y=180)
    ass = tmp_path / "t.ass"
    ass.write_text(script, encoding="utf-8")
    out = tmp_path / "t.mp4"
    escaped = ass.as_posix().replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=320x180:d=3",
            "-vf", f"subtitles={escaped}",
            "-frames:v", "30", str(out),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-800:]
    assert out.stat().st_size > 0
