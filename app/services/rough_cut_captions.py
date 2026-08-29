"""Styled caption rendering for the rough-cut export: CaptionStyle → ASS.

The editor has a ~200-field caption engine; the export used to burn a bare
SRT at libass defaults — the whole caption look vanished between preview and
MP4 (plan §5.3 #1, the highest-blast-radius parity gap). This module maps the
editor's `CaptionStyle` onto an ASS script libass can burn:

- typography: family (resolved to a real name client-side), weight, italic,
  size as % of frame height × scale, letter spacing, uppercase;
- layout: position row × text alignment column → ASS alignment 1–9, margins
  from the x/y offsets, line breaking from `wordsPerLine` × `lines`;
- color: fill/activeFill with opacity folded into the alpha channel;
- the active-word highlight as karaoke: `\\kf` (sweep) for `progressive`,
  `\\k` (per-word switch) for the other styles.

What ASS cannot express — pill/halo geometry, per-word scale bumps, keyword
treatments, glow — degrades to its nearest ASS form and is REPORTED in the
returned warnings, never silently dropped. The export row's meta carries them
so the UI can say exactly how the burn differs from the preview.

Word timing comes from the transcription's word list, remapped to export time
by the caller; an entry without words renders as a plain styled line.
"""

from __future__ import annotations

import re
from typing import Any

#: highlight styles whose ASS form is per-word karaoke.
_KARAOKE_STYLES = {"pill", "block", "fill", "underline", "halo", "progressive"}

_POSITION_ROW = {"top": 2, "center": 1, "bottom": 0}  # keypad row offset ×3
_ALIGN_COLUMN = {"left": 1, "center": 2, "right": 3}


def _num(value: Any, fallback: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return fallback
    if result != result or result in (float("inf"), float("-inf")):
        return fallback
    return result


def _hex_to_ass(color: Any, fallback: str, *, opacity_pct: float = 100.0) -> str:
    """#RRGGBB[AA] (CSS) → &HAABBGGRR (ASS, alpha 00 = opaque)."""
    text = str(color or "").strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", text):
        text = fallback.lstrip("#")
    r, g, b = text[0:2], text[2:4], text[4:6]
    css_alpha = int(text[6:8], 16) / 255.0 if len(text) == 8 else 1.0
    combined = max(0.0, min(1.0, css_alpha * (_num(opacity_pct, 100.0) / 100.0)))
    ass_alpha = int(round((1.0 - combined) * 255))
    return f"&H{ass_alpha:02X}{b.upper()}{g.upper()}{r.upper()}"


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\r", " ")
        .replace("\n", r"\N")
    )


def _alignment(style: dict[str, Any]) -> int:
    row = _POSITION_ROW.get(str(style.get("position") or "bottom"), 0)
    column = _ALIGN_COLUMN.get(str(style.get("textAlign") or "center"), 2)
    return row * 3 + column


def _chunk_words(
    words: list[dict[str, Any]], words_per_line: int, lines: int
) -> list[list[list[dict[str, Any]]]]:
    """Blocks of `lines` lines of `words_per_line` words — the editor's grouping."""
    words_per_line = max(1, int(words_per_line))
    lines = max(1, int(lines))
    per_block = words_per_line * lines
    blocks: list[list[list[dict[str, Any]]]] = []
    for index in range(0, len(words), per_block):
        chunk = words[index : index + per_block]
        block = [
            chunk[line_start : line_start + words_per_line]
            for line_start in range(0, len(chunk), words_per_line)
        ]
        blocks.append(block)
    return blocks


def _entry_words(entry: dict[str, Any]) -> list[dict[str, Any]]:
    words = entry.get("words")
    out: list[dict[str, Any]] = []
    if isinstance(words, list):
        for word in words:
            if not isinstance(word, dict):
                continue
            text = str(word.get("word") or word.get("text") or "").strip()
            start = _num(word.get("start"), -1.0)
            end = _num(word.get("end"), -1.0)
            if text and end > start >= 0:
                out.append({"word": text, "start": start, "end": end})
    return out


def _karaoke_block_text(
    lines: list[list[dict[str, Any]]],
    block_start: float,
    *,
    sweep: bool,
    uppercase: bool,
) -> str:
    """Per-word `\\k`/`\\kf` tags across a whole block.

    Karaoke timing inside one Dialogue event is CUMULATIVE through the entire
    text — a `\\N` line break does not reset the clock — so the block is timed
    as one continuous word sequence with breaks inserted at line boundaries.
    Each tag's duration runs to the NEXT word's start, so the highlight lands
    on word boundaries rather than drifting by the inter-word gaps.
    """
    tag = r"\kf" if sweep else r"\k"
    flat = [word for line in lines for word in line]
    if not flat:
        return ""
    line_break_after = set()
    seen = 0
    for line in lines[:-1]:
        seen += len(line)
        line_break_after.add(seen - 1)

    parts: list[str] = []
    lead_cs = int(round(max(0.0, flat[0]["start"] - block_start) * 100))
    if lead_cs > 0:
        parts.append(f"{{{tag}{lead_cs}}}")
    for index, word in enumerate(flat):
        next_start = flat[index + 1]["start"] if index + 1 < len(flat) else word["end"]
        duration_cs = max(1, int(round((next_start - word["start"]) * 100)))
        text = _escape(word["word"])
        if uppercase:
            text = text.upper()
        separator = r"\N" if index in line_break_after else " "
        parts.append(f"{{{tag}{duration_cs}}}{text}{separator if index + 1 < len(flat) else ''}")
    return "".join(parts)


def build_ass(
    entries: list[dict[str, Any]],
    style: dict[str, Any],
    *,
    play_res_x: int,
    play_res_y: int,
) -> tuple[str, list[str]]:
    """Render caption entries (export time) as an ASS script.

    Entries: `{"start", "end", "text", "words"?: [{word,start,end}, …]}`.
    Returns `(script, warnings)`; warnings name every preview feature that
    degraded to an ASS approximation.
    """
    warnings: list[str] = []

    uppercase = bool(style.get("uppercase"))
    italic = 1 if style.get("italic") else 0
    weight = _num(style.get("fontWeight"), 700)
    bold = 1 if weight >= 600 else 0
    font = str(style.get("fontFamily") or "Inter").strip() or "Inter"

    size_pct = _num(style.get("fontSize"), 4.4)
    scale_pct = _num(style.get("scale"), 100.0)
    size = max(8, int(round(play_res_y * (size_pct / 100.0) * (scale_pct / 100.0))))
    spacing = round(_num(style.get("letterSpacing"), 0.0) * size, 1)

    opacity = _num(style.get("opacity"), 100.0)
    fill = _hex_to_ass(style.get("fill"), "#FFFFFF", opacity_pct=opacity)
    active_fill = _hex_to_ass(style.get("activeFill"), "#FFD400", opacity_pct=opacity)

    highlight = str(style.get("highlightStyle") or "none")
    karaoke = highlight in _KARAOKE_STYLES
    if highlight in {"pill", "block", "halo"}:
        warnings.append(
            f"Caption '{highlight}' highlight is rendered as a colour highlight in the "
            "export — ASS has no per-word box geometry."
        )
    # In karaoke, already-sung text holds PrimaryColour and unsung text shows
    # SecondaryColour — so Primary carries the active colour.
    primary = active_fill if karaoke else fill
    inactive_opacity = _num(style.get("inactiveOpacity"), 100.0)
    secondary = _hex_to_ass(
        style.get("fill"),
        "#FFFFFF",
        opacity_pct=opacity * (inactive_opacity / 100.0)
        if highlight == "progressive"
        else opacity,
    )

    stroke_on = bool(style.get("stroke", True))
    outline_value = round(_num(style.get("strokeWidth"), 2.0), 1) if stroke_on else 0.0
    outline_w = f"{outline_value:g}"
    outline = _hex_to_ass(style.get("strokeColor"), "#000000", opacity_pct=opacity)

    backdrop = str(style.get("backdrop") or "none")
    border_style = 1
    back_color = "&H00000000"
    if backdrop != "none":
        border_style = 3
        back_color = _hex_to_ass(
            style.get("backdropColor"),
            "#000000",
            opacity_pct=_num(style.get("backdropOpacity"), 60.0),
        )
        warnings.append(
            f"Caption '{backdrop}' backdrop is rendered as an opaque box in the export "
            "— ASS has no rounded plates."
        )

    if _num(style.get("glowIntensity"), 0.0) > 0:
        warnings.append("Caption glow does not render in the export.")
    if style.get("keywordFontId") or style.get("keywordFill") or style.get("keywordBackground"):
        warnings.append("Keyword emphasis styling does not render in the export.")
    if abs(_num(style.get("rotation"), 0.0)) > 0.01:
        warnings.append("Caption rotation does not render in the export.")

    alignment = _alignment(style)
    # Margins: a base inset plus the editor's percentage offsets.
    margin_v = int(round(play_res_y * 0.06 + abs(_num(style.get("yOffset"), 0.0)) / 100.0 * play_res_y * 0.5))
    margin_h = int(round(play_res_x * 0.04 + abs(_num(style.get("xOffset"), 0.0)) / 100.0 * play_res_x * 0.5))

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {play_res_x}\n"
        f"PlayResY: {play_res_y}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour,"
        " Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline,"
        " Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font},{size},{primary},{secondary},{outline},{back_color},"
        f"{bold},{italic},0,0,100,100,{spacing},0,{border_style},{outline_w},0,"
        f"{alignment},{margin_h},{margin_h},{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    words_per_line = int(_num(style.get("wordsPerLine"), 4))
    lines_per_block = int(_num(style.get("lines"), 1))
    underline_tag = r"{\u1}" if highlight == "underline" else ""
    sweep = highlight == "progressive"

    events: list[str] = []
    for entry in entries:
        start = _num(entry.get("start"), -1.0)
        end = _num(entry.get("end"), -1.0)
        if end <= start or start < 0:
            continue
        words = _entry_words(entry)
        if karaoke and words:
            for block in _chunk_words(words, words_per_line, lines_per_block):
                block_words = [word for line in block for word in line]
                if not block_words:
                    continue
                block_start = max(start, min(w["start"] for w in block_words))
                block_end = min(end, max(w["end"] for w in block_words))
                if block_end <= block_start:
                    continue
                text = underline_tag + _karaoke_block_text(
                    block, block_start, sweep=sweep, uppercase=uppercase
                )
                events.append(
                    f"Dialogue: 0,{_ass_time(block_start)},{_ass_time(block_end)},Default,,0,0,0,,{text}"
                )
        else:
            text = _escape(str(entry.get("text") or "").strip())
            if not text:
                continue
            if uppercase:
                text = text.upper()
            events.append(
                f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{underline_tag}{text}"
            )

    return header + "\n".join(events) + ("\n" if events else ""), warnings
