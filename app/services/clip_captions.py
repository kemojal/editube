"""
Caption/subtitle generation for clips.

Takes a VideoTranscription (segments JSONB) + a ClipStyle, filters to the clip
time range, groups into lines per style, and emits ASS, SRT, or VTT.

Editube transcriptions do not carry word-level timestamps, so we operate at
segment granularity; the segment's text is chunked into `words_per_line` lines
that share the segment's [start, end] time window.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass
class CaptionBlock:
    start: float  # seconds, clip-relative
    end: float
    lines: list[str]


def _chunk_words(text: str, words_per_line: int, max_lines: int) -> list[str]:
    words = [w for w in (text or "").split() if w]
    if not words:
        return []
    per_line = max(1, int(words_per_line))
    lines = [
        " ".join(words[i : i + per_line]) for i in range(0, len(words), per_line)
    ]
    return lines[: max(1, int(max_lines))]


def blocks_from_segments(
    segments: Iterable[dict[str, Any]],
    *,
    clip_start: float,
    clip_end: float,
    words_per_line: int = 3,
    max_lines: int = 2,
    uppercase: bool = False,
) -> list[CaptionBlock]:
    out: list[CaptionBlock] = []
    for seg in segments or []:
        try:
            s = float(seg.get("start", 0.0))
            e = float(seg.get("end", 0.0))
            text = str(seg.get("text", "")).strip()
        except (TypeError, ValueError):
            continue
        if e <= clip_start or s >= clip_end:
            continue
        rel_start = max(0.0, s - clip_start)
        rel_end = max(rel_start + 0.1, min(e, clip_end) - clip_start)
        if uppercase:
            text = text.upper()
        lines = _chunk_words(text, words_per_line, max_lines)
        if lines:
            out.append(CaptionBlock(rel_start, rel_end, lines))
    return out


def blocks_from_cuts(
    segments: Iterable[dict[str, Any]],
    *,
    cuts: list[dict[str, float]],
    words_per_line: int = 3,
    max_lines: int = 2,
    uppercase: bool = False,
) -> list[CaptionBlock]:
    """Caption blocks where each kept range is mapped onto the concat timeline.

    For a cuts list `[[s1,e1], [s2,e2], ...]`, the rendered video plays the
    ranges back-to-back. Caption start times must be shifted by the running
    total of prior kept-range durations so they line up on the output clock.
    Segments that fall entirely inside removed gaps are dropped; segments
    straddling a boundary are split into pieces per intersecting range.
    """
    if not cuts:
        return []

    segs = list(segments or [])
    out: list[CaptionBlock] = []
    offset = 0.0
    for cut in cuts:
        cs = float(cut["start"])
        ce = float(cut["end"])
        if ce <= cs:
            continue
        range_dur = ce - cs
        for seg in segs:
            try:
                s = float(seg.get("start", 0.0))
                e = float(seg.get("end", 0.0))
                text = str(seg.get("text", "")).strip()
            except (TypeError, ValueError):
                continue
            if e <= cs or s >= ce:
                continue
            clipped_s = max(s, cs)
            clipped_e = min(e, ce)
            rel_start = offset + (clipped_s - cs)
            rel_end = max(rel_start + 0.1, offset + (clipped_e - cs))
            if uppercase:
                text = text.upper()
            lines = _chunk_words(text, words_per_line, max_lines)
            if lines:
                out.append(CaptionBlock(rel_start, rel_end, lines))
        offset += range_dur
    return out


def _fmt_srt(t: float) -> str:
    if t < 0:
        t = 0.0
    hh = int(t // 3600)
    mm = int((t % 3600) // 60)
    ss = int(t % 60)
    ms = int(round((t - math.floor(t)) * 1000))
    if ms == 1000:
        ms, ss = 0, ss + 1
    return f"{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}"


def _fmt_vtt(t: float) -> str:
    return _fmt_srt(t).replace(",", ".")


def _fmt_ass(t: float) -> str:
    if t < 0:
        t = 0.0
    hh = int(t // 3600)
    mm = int((t % 3600) // 60)
    ss = t % 60
    return f"{hh:01d}:{mm:02d}:{ss:05.2f}"


def to_srt(blocks: list[CaptionBlock]) -> str:
    out = []
    for i, b in enumerate(blocks, 1):
        out.append(str(i))
        out.append(f"{_fmt_srt(b.start)} --> {_fmt_srt(b.end)}")
        out.extend(b.lines)
        out.append("")
    return "\n".join(out)


def to_vtt(blocks: list[CaptionBlock]) -> str:
    out = ["WEBVTT", ""]
    for b in blocks:
        out.append(f"{_fmt_vtt(b.start)} --> {_fmt_vtt(b.end)}")
        out.extend(b.lines)
        out.append("")
    return "\n".join(out)


def _hex_to_ass_color(hex_color: str | None, default: str = "&H00FFFFFF") -> str:
    if not hex_color:
        return default
    s = hex_color.lstrip("#")
    if len(s) != 6:
        return default
    try:
        r = int(s[0:2], 16)
        g = int(s[2:4], 16)
        b = int(s[4:6], 16)
    except ValueError:
        return default
    # ASS is &HAABBGGRR (alpha 00 = opaque)
    return f"&H00{b:02X}{g:02X}{r:02X}"


_POS_TO_ALIGN = {
    "top": 8,       # top-center
    "center": 5,    # middle-center
    "bottom": 2,    # bottom-center
}


def to_ass(
    blocks: list[CaptionBlock],
    style: dict[str, Any],
    *,
    play_res_x: int = 1080,
    play_res_y: int = 1920,
) -> str:
    font = str(style.get("caption_font") or "Inter")
    size = int(style.get("caption_size") or 56)
    primary = _hex_to_ass_color(style.get("caption_color"), "&H00FFFFFF")
    outline = _hex_to_ass_color(style.get("caption_stroke_color"), "&H00000000")
    outline_w = int(style.get("caption_stroke_width") or 2)
    bold = 1 if str(style.get("caption_font_weight") or "700") in {"700", "800", "900", "bold"} else 0
    align = _POS_TO_ALIGN.get(str(style.get("caption_position") or "bottom"), 2)

    pos_y = style.get("caption_position_y")
    pos_x = style.get("caption_position_x")
    pos_override = ""
    if pos_y is not None and pos_x is not None:
        try:
            px = int(float(pos_x) / 100.0 * play_res_x)
            py = int(float(pos_y) / 100.0 * play_res_y)
            pos_override = f"{{\\an{align}\\pos({px},{py})}}"
        except (TypeError, ValueError):
            pos_override = ""

    highlight_style = str(style.get("caption_highlight_style") or "")
    highlight_hex = style.get("caption_highlight_color")
    highlight_ass = _hex_to_ass_color(highlight_hex, "&H0000FFFF") if highlight_hex else None

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {play_res_x}\n"
        f"PlayResY: {play_res_y}\n"
        "WrapStyle: 2\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour,"
        " Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline,"
        " Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font},{size},{primary},&H000000FF,{outline},&H00000000,"
        f"{bold},0,0,0,100,100,0,0,1,{outline_w},0,{align},40,40,80,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    events: list[str] = []
    for b in blocks:
        text = "\\N".join(b.lines)
        if highlight_style == "color" and highlight_ass:
            text = f"{{\\c{highlight_ass}}}{text}"
        elif highlight_style == "underline":
            text = f"{{\\u1}}{text}"
        if pos_override:
            text = f"{pos_override}{text}"
        events.append(
            f"Dialogue: 0,{_fmt_ass(b.start)},{_fmt_ass(b.end)},Default,,0,0,0,,{text}"
        )

    return header + "\n".join(events) + "\n"
