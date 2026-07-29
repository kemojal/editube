"""Python mirror of the browser's text-mask layout
(`editube-frontend/app/(sites)/dashboard/rough-cut/_lib/mask/mask-text.ts`,
plus the font registry in `mask-fonts.ts`).

Both renderers RASTERISE text — the editor with SVG `<text>`/`<tspan>`, this
side with `PIL.ImageDraw.text` + `ImageFont.truetype` — from the SAME TTF
file. Neither traces outlines. Everything that decides *where* a glyph run
lands is computed here from numbers only (no font metrics), so the two
languages cannot disagree about layout even though they rasterise glyphs
with different engines. Parity is asserted by `tests/test_mask_text.py`
against the shared golden fixture
`editube-frontend/docs/fixtures/mask-text-golden.json` (vendored copy in
`tests/fixtures/`, with a sha256 drift check — the same idiom the geometry
and expansion fixtures use).

TWO RULES THAT MUST NOT REGRESS
-------------------------------
1. **Line spacing is ours, not the toolkit's.** SVG has no line-height for
   `<text>`; Pillow's `multiline_text` has its own (`font.getmetrics()`
   ascent+descent plus `spacing`). Those two ladders differ per font, so
   NEITHER is used: `line_step = font_size_vb * (line_spacing / 100)`, one
   absolute baseline per line, one `draw.text` call per line. Never call
   `multiline_text` here.
2. **Position by BASELINE, never by an ink bounding box.** SVG positions
   text by its baseline. Pillow's default anchor ("la") is the ascender top
   and `textbbox` reports the ink box, which moves with the glyphs ("ao" vs
   "Ay" have different ink boxes for identical layout). Mixing them is the
   classic way exported text sits a few percent off the preview. Every
   `draw.text` call in `render_text_layer` therefore passes an explicit
   baseline anchor ("ls"/"ms"/"rs"), and no `getbbox`/`textbbox` value ever
   reaches a coordinate.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import ImageDraw, ImageFont

from app.services.mask_geometry import VIEWBOX, resolve_box, sample_mask_channel, sample_mask_transform

logger = logging.getLogger(__name__)

# --- Font registry (mirrors mask-fonts.ts) ---------------------------------
#
# The same TTFs the frontend serves from `public/fonts/mask-text/`, vendored
# here because the two apps deploy independently (per CLAUDE.md each inner
# directory becomes its own git root, so a cross-repo path would not exist in
# production). `tests/test_mask_text.py` sha256s the pair so the copies cannot
# drift apart silently.
MASK_FONT_DIR = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "mask-text"

MASK_FONTS: dict[str, dict[str, Any]] = {
    "vera-sans": {
        "label": "Vera Sans",
        "files": {
            "regular": "Vera.ttf",
            "bold": "VeraBd.ttf",
            "italic": "VeraIt.ttf",
            "boldItalic": "VeraBI.ttf",
        },
    },
}

DEFAULT_MASK_FONT_ID = "vera-sans"

# Fraction of the line box above the baseline. A shared constant, NOT the
# font's own ascent -- the only way SVG and Pillow can agree without both
# parsing hhea/OS2 and rounding identically. 0.8/0.2 sits close to Bitstream
# Vera's own ratio (1901/2048 vs 483/2048).
TEXT_ASCENT_RATIO = 0.8
TEXT_DESCENT_RATIO = 1 - TEXT_ASCENT_RATIO

# Underline geometry as a fraction of the font size, taken from Vera's `post`
# table (underlinePosition -213/2048 em, underlineThickness 143/2048 em) so
# the rule Pillow draws lands where the browser's `text-decoration:underline`
# puts it.
TEXT_UNDERLINE_OFFSET = 213 / 2048
TEXT_UNDERLINE_THICKNESS = 143 / 2048

TEXT_MAX_LINES = 24

_ANCHOR_H = {"left": "l", "center": "m", "right": "r"}
_SVG_ANCHOR = {"left": "start", "center": "middle", "right": "end"}


def mask_font_style_key(bold: bool, italic: bool) -> str:
    if bold and italic:
        return "boldItalic"
    if bold:
        return "bold"
    if italic:
        return "italic"
    return "regular"


def resolve_mask_font(font_id: str | None) -> tuple[str, bool]:
    """Returns `(font_id_used, fallback_applied)`.

    Never substitutes silently: callers surface `fallback_applied` in the
    export job's warnings so a user whose font we do not ship is told the
    export used a different typeface rather than discovering it in the MP4.
    """
    requested = font_id or DEFAULT_MASK_FONT_ID
    if requested in MASK_FONTS:
        return requested, False
    return DEFAULT_MASK_FONT_ID, True


def mask_font_path(font_id: str, style_key: str) -> Path:
    return MASK_FONT_DIR / MASK_FONTS[font_id]["files"][style_key]


@dataclass(frozen=True)
class MaskTextLine:
    text: str
    x: float
    """VIEWBOX x of this line's anchor point (meaning depends on the anchor)."""
    baseline_y: float
    """VIEWBOX y of this line's BASELINE."""


@dataclass(frozen=True)
class MaskTextLayout:
    font_id: str
    requested_font_id: str
    font_fallback: bool
    style_key: str
    bold: bool
    italic: bool
    underline: bool
    font_size_vb: float
    line_step: float
    letter_spacing_vb: float
    align: str
    align_v: str
    anchor: str
    """SVG `text-anchor` value; `pillow_anchor` derives the PIL one."""
    underline_offset_vb: float
    underline_thickness_vb: float
    glyph_scale_x: float
    """Horizontal pre-compensation the SVG side applies -- see
    `maskTextGlyphTransform` in mask-text.ts. Always `1 / frame_aspect`.

    Nothing here multiplies by it: Pillow has no non-uniform CTM to cancel,
    it simply draws isotropic glyphs, which is the shape that compensation
    makes the browser produce too. It is carried on the layout (and asserted
    in the golden fixture at two different frame aspects) so a regression
    that drops the browser-side correction fails a test in BOTH languages
    instead of only showing up as fat letters in someone's preview.
    """
    rotation: float
    centre_x: float
    centre_y: float
    lines: list[MaskTextLine] = field(default_factory=list)

    @property
    def pillow_anchor(self) -> str:
        """Horizontal anchor + baseline row -- see module rule 2."""
        return _ANCHOR_H[self.align] + "s"


def mask_text_lines(text: str) -> list[str]:
    """Splits exactly like the browser's textarea produces newlines."""
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")[:TEXT_MAX_LINES]


def mask_text_layout(mask: dict[str, Any], t: float, frame_aspect: float) -> MaskTextLayout | None:
    """Mirrors `maskTextLayout` in mask-text.ts. Returns None when there is
    nothing to draw (an empty textarea paints nothing, it does not black out
    the frame)."""
    raw = mask.get("text") or ""
    lines = mask_text_lines(str(raw))
    if not any(line.strip() for line in lines):
        return None

    transform = sample_mask_transform(mask, t)
    box = resolve_box(mask, transform, frame_aspect)

    # `zoom` is one of the nine keyframe channels -- sampled through the same
    # per-channel machinery as x/y/width/height, never a parallel path.
    zoom = sample_mask_channel(mask, "zoom", t)
    font_size = float(mask.get("fontSize") if mask.get("fontSize") is not None else 20)
    font_size_vb = (font_size / 100.0) * VIEWBOX * (zoom / 100.0)
    if font_size_vb <= 0:
        return None

    line_spacing = float(mask.get("lineSpacing") if mask.get("lineSpacing") is not None else 120)
    line_step = font_size_vb * (line_spacing / 100.0)
    letter_spacing = float(mask.get("letterSpacing") or 0)
    letter_spacing_vb = (letter_spacing / 100.0) * font_size_vb

    align = mask.get("align") if mask.get("align") in _ANCHOR_H else "center"
    align_v = mask.get("alignV") if mask.get("alignV") in ("top", "middle", "bottom") else "middle"

    block_height = (len(lines) - 1) * line_step + font_size_vb
    if align_v == "top":
        block_top = box.top
    elif align_v == "bottom":
        block_top = box.top + box.height - block_height
    else:
        block_top = box.centre_y - block_height / 2

    first_baseline = block_top + TEXT_ASCENT_RATIO * font_size_vb
    if align == "left":
        x = box.left
    elif align == "right":
        x = box.left + box.width
    else:
        x = box.centre_x

    font_id, fallback = resolve_mask_font(mask.get("fontId"))
    bold = mask.get("bold") is True
    italic = mask.get("italic") is True

    return MaskTextLayout(
        font_id=font_id,
        requested_font_id=mask.get("fontId") or DEFAULT_MASK_FONT_ID,
        font_fallback=fallback,
        style_key=mask_font_style_key(bold, italic),
        bold=bold,
        italic=italic,
        underline=mask.get("underline") is True,
        font_size_vb=font_size_vb,
        line_step=line_step,
        letter_spacing_vb=letter_spacing_vb,
        align=align,
        align_v=align_v,
        anchor=_SVG_ANCHOR[align],
        underline_offset_vb=TEXT_UNDERLINE_OFFSET * font_size_vb,
        underline_thickness_vb=TEXT_UNDERLINE_THICKNESS * font_size_vb,
        glyph_scale_x=1 / frame_aspect,
        rotation=transform.rotation,
        centre_x=box.centre_x,
        centre_y=box.centre_y,
        lines=[
            MaskTextLine(text=line, x=x, baseline_y=first_baseline + index * line_step)
            for index, line in enumerate(lines)
        ],
    )


def viewbox_rotation_affine(
    angle_deg: float, centre_px: tuple[float, float], sx: float, sy: float
) -> tuple[float, float, float, float, float, float]:
    """Inverse-mapping AFFINE coefficients for a rotation performed in
    VIEWBOX space, expressed in output pixels.

    Every other shape rotates in VIEWBOX space in BOTH languages: the TS side
    rotates path coordinates before the mask's non-uniform
    objectBoundingBox CTM stretches them, and `_rotate_points` in
    mask_geometry.py rotates polygon vertices before `_scale_points`. Text
    has no vertices to rotate -- it is rasterised -- so the drawn layer must
    be transformed instead, and a plain `Image.rotate` would rotate in PIXEL
    space, which is NOT the same thing when `sx != sy` (a rotated block would
    sit at a different angle in the export than in the preview).

    Pixel-space equivalent of a VIEWBOX-space rotation R is `S R S^-1` with
    `S = diag(sx, sy)`. `Image.transform(..., AFFINE, data)` wants the
    INVERSE map (output -> input), so this returns `S R(-angle) S^-1` about
    `centre_px`.
    """
    theta = math.radians(angle_deg)
    cos_a, sin_a = math.cos(theta), math.sin(theta)
    # S · R(-θ) · S⁻¹ , with R(-θ) = [[cos, sin], [-sin, cos]] (y-down, so a
    # positive angle is clockwise -- matching SVG's rotate()).
    a = cos_a
    b = (sx / sy) * sin_a
    d = -(sy / sx) * sin_a
    e = cos_a
    cx, cy = centre_px
    return (a, b, cx - (a * cx + b * cy), d, e, cy - (d * cx + e * cy))


def _line_advance(font: ImageFont.FreeTypeFont, text: str, letter_spacing_px: float) -> float:
    """Advance width of one line, INCLUDING a trailing letter-space.

    CSS `letter-spacing` adds its space after every character including the
    last, and that trailing space is part of the advance the browser centres
    or right-aligns against. Matching it here is what keeps a centred,
    letter-spaced line from sitting half a space off in the export.
    """
    if letter_spacing_px == 0:
        return font.getlength(text)
    return sum(font.getlength(ch) for ch in text) + letter_spacing_px * len(text)


def render_text_layer(
    draw: ImageDraw.ImageDraw,
    mask: dict[str, Any],
    t: float,
    frame_aspect: float,
    sx: float,
    sy: float,
    colour: int,
) -> MaskTextLayout | None:
    """Draws a text mask's glyphs into `draw`'s image. Returns the layout (so
    the caller can report a font fallback), or None if nothing was drawn.

    `sx`/`sy` scale VIEWBOX units onto output pixels. The font is loaded at
    the **y**-scaled size and glyphs are drawn ISOTROPICALLY: `fontSize` is a
    percentage of frame height, so height is what it must control, and a
    glyph's own proportions must not depend on the frame's aspect. The
    browser reaches the same result by pre-dividing its `<text>` group by
    `1 / frame_aspect` to cancel the mask's non-uniform objectBoundingBox
    CTM (`maskTextGlyphTransform` in mask-text.ts) -- without that
    compensation the preview stretched letters ~78% wider on a 16:9 frame
    while this side never did.

    Consequently every horizontal quantity here is scaled by `sy` as well,
    not `sx`: letter-spacing and the underline rule are fractions of the font
    size, and after the browser's compensation they render at `L * sx * (sy /
    sx) = L * sy` px there.
    """
    layout = mask_text_layout(mask, t, frame_aspect)
    if layout is None:
        return None

    font_size_px = max(1, round(layout.font_size_vb * sy))
    try:
        font = ImageFont.truetype(str(mask_font_path(layout.font_id, layout.style_key)), font_size_px)
    except OSError:
        logger.exception("mask text: could not load font %s/%s", layout.font_id, layout.style_key)
        return layout

    # `sy`, not `sx` -- see the docstring: letter-spacing is a fraction of the
    # font size, which is y-scaled, and that is what the compensated SVG side
    # renders too.
    letter_spacing_px = layout.letter_spacing_vb * sy
    anchor = layout.pillow_anchor

    for line in layout.lines:
        if not line.text:
            continue
        x_px = line.x * sx
        y_px = line.baseline_y * sy
        width_px = _line_advance(font, line.text, letter_spacing_px)

        if letter_spacing_px == 0:
            draw.text((x_px, y_px), line.text, font=font, fill=colour, anchor=anchor)
        else:
            # Pillow cannot letter-space a run, so the line is walked
            # character by character. Kerning is lost by doing that -- which
            # is exactly why the SVG side sets `font-kerning: none` whenever
            # letter-spacing is non-zero: both renderers then walk the same
            # per-character advances.
            if layout.align == "left":
                start = x_px
            elif layout.align == "right":
                start = x_px - width_px
            else:
                start = x_px - width_px / 2
            pen = start
            for ch in line.text:
                draw.text((pen, y_px), ch, font=font, fill=colour, anchor="ls")
                pen += font.getlength(ch) + letter_spacing_px

        if layout.underline:
            # Drawn by hand from the font's published underline metrics --
            # Pillow has no text-decoration. Same offset/thickness constants
            # the browser's own underline uses for this family.
            offset = layout.underline_offset_vb * sy
            thickness = max(1.0, layout.underline_thickness_vb * sy)
            if layout.align == "left":
                x0 = x_px
            elif layout.align == "right":
                x0 = x_px - width_px
            else:
                x0 = x_px - width_px / 2
            y0 = y_px + offset
            draw.rectangle([x0, y0, x0 + width_px, y0 + thickness], fill=colour)

    return layout
