"""Server-rendered label cards for tracked callouts.

A tracked callout has to MOVE, and the burn-in channel is a static PNG — so
the label ships as a first-class generated image on the timeline instead,
where the export's keyframed `video.x`/`video.y` expressions (and the
viewer's own layer transforms) can carry it along the track. This module
renders that card: Pillow, using the same bundled font family the mask-text
engine resolves (`vera-sans` — the one family every worker actually has), a
rounded translucent plate, and a side notch pointing at the subject.

Pure-ish by design: `render_label_card` returns PNG bytes + dimensions and
touches no storage; the executor owns the upload and the `GeneratedMedia`
row, and tests fake this function the way they fake the segmentation job.
"""

from __future__ import annotations

from io import BytesIO

#: Render at 2x for crisp downscaling on the stage.
_SCALE = 2
_FONT_SIZE = 22 * _SCALE
_PAD_X = 18 * _SCALE
_PAD_Y = 12 * _SCALE
_RADIUS = 10 * _SCALE
_NOTCH = 10 * _SCALE

_PLATE = (17, 17, 22, 230)
_BORDER = (255, 255, 255, 46)
_TEXT = (255, 255, 255, 255)


def render_label_card(
    text: str,
    *,
    side: str = "left",
    accent: str | None = None,
) -> tuple[bytes, int, int]:
    """One label card as PNG bytes, plus its pixel size.

    `side` is which side of the SUBJECT the card sits on — the notch points
    the other way, at the subject. `accent` (a #RRGGBB) colours a small
    leading tick, the one brandable element.
    """
    from PIL import Image, ImageDraw, ImageFont

    from app.services.mask_text import mask_font_path, resolve_mask_font

    label = (text or "Label").strip()[:48]
    font_id, _fallback = resolve_mask_font(None)
    font = ImageFont.truetype(str(mask_font_path(font_id, "bold")), _FONT_SIZE)

    probe = Image.new("RGBA", (8, 8))
    draw = ImageDraw.Draw(probe)
    text_w = int(draw.textlength(label, font=font))
    text_h = int(_FONT_SIZE * 1.2)

    accent_w = (6 * _SCALE + 8 * _SCALE) if accent else 0
    plate_w = text_w + accent_w + _PAD_X * 2
    plate_h = text_h + _PAD_Y * 2
    width = plate_w + _NOTCH
    height = plate_h

    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    plate_x = _NOTCH if side == "right" else 0
    draw.rounded_rectangle(
        (plate_x, 0, plate_x + plate_w - 1, plate_h - 1),
        radius=_RADIUS,
        fill=_PLATE,
        outline=_BORDER,
        width=_SCALE,
    )

    # The notch: a small triangle pointing at the subject.
    mid = plate_h // 2
    if side == "right":
        draw.polygon([(0, mid), (_NOTCH, mid - _NOTCH), (_NOTCH, mid + _NOTCH)], fill=_PLATE)
    else:
        draw.polygon(
            [(width - 1, mid), (plate_w - 1, mid - _NOTCH), (plate_w - 1, mid + _NOTCH)],
            fill=_PLATE,
        )

    cursor = plate_x + _PAD_X
    if accent:
        try:
            rgb = tuple(int(accent.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            rgb = (255, 212, 0)
        tick_h = int(text_h * 0.7)
        top = (plate_h - tick_h) // 2
        draw.rounded_rectangle(
            (cursor, top, cursor + 6 * _SCALE, top + tick_h),
            radius=3 * _SCALE,
            fill=(*rgb, 255),
        )
        cursor += accent_w

    # `ms` anchor: middle of the ascender band — the same metric-free baseline
    # convention the mask-text renderer uses, so Pillow and the preview agree.
    draw.text((cursor, plate_h // 2), label, font=font, fill=_TEXT, anchor="lm")

    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue(), width, height
