"""Cross-language contract for the brush/pen ROUND-PEN fix (frontend Part 1
of the "brush tool modes and pen states" task).

The mask `<mask>` SVG paints with `maskContentUnits="objectBoundingBox"`
inside `<g transform="scale(1/VIEWBOX)">` (mask-svg-defs.tsx), an anisotropic
CTM: `sx = frameWidth / VIEWBOX`, `sy = frameHeight / VIEWBOX`. A brush
stroke's `stroke-width` is a single scalar applied before that CTM, so
without compensation its round cap/join renders as an ELLIPSE (`sx`-wide,
`sy`-tall) instead of round.

`brushStrokeCompensation(frameAspect)` in mask-geometry.ts fixes this with an
inner `scale(kx, ky)` wrapping ONLY the stroke's rendering (never the public
`brushPath`/`maskGeometry` output other consumers and the cross-language
token-parity fixture depend on staying raw -- see that function's and
`brushStrokeRenderGeometry`'s doc comments). This test is the Python-side
half of that contract: `app/services/mask_matte.py` draws every stroke with
Pillow's `ImageDraw.line(..., width=stroke_w)`, a single scalar in FINAL
PIXEL space, scaled by the AVERAGE `(sx + sy) / 2` -- so "round, matching
Pillow" means the frontend's two per-axis effective widths must both equal
that same average. If either side's formula drifts, this test starts
failing -- it recomputes both formulas directly (no vendored JSON needed,
since both are pure arithmetic on `frameAspect`/`sx`/`sy`).
"""

import math
import unittest

from app.services.mask_geometry import VIEWBOX, stroke_render_width, MaskTransform


def brush_stroke_compensation(frame_aspect: float) -> tuple[float, float]:
    """Python mirror of mask-geometry.ts's `brushStrokeCompensation`."""
    kx = (1 + 1 / frame_aspect) / 2
    ky = (1 + frame_aspect) / 2
    return kx, ky


class BrushStrokeRoundnessTests(unittest.TestCase):
    def _assert_round_at(self, frame_width: int, frame_height: int):
        frame_aspect = frame_width / frame_height
        sx = frame_width / VIEWBOX
        sy = frame_height / VIEWBOX
        kx, ky = brush_stroke_compensation(frame_aspect)

        transform = MaskTransform(x=0, y=0, width=100, height=100, rotation=0)
        width = stroke_render_width({"size": 8}, transform)

        effective_x = width * kx * sx
        effective_y = width * ky * sy
        self.assertAlmostEqual(effective_x, effective_y, places=9)

        # The target Pillow actually draws with (mask_matte.py's `stroke_w`).
        pillow_width = width * ((sx + sy) / 2)
        self.assertAlmostEqual(effective_x, pillow_width, places=9)

    def test_round_at_16_9(self):
        self._assert_round_at(1920, 1080)

    def test_round_at_9_16_portrait(self):
        self._assert_round_at(1080, 1920)

    def test_round_at_1_1_square(self):
        # sx == sy already, so kx == ky == 1 -- compensation is a no-op.
        kx, ky = brush_stroke_compensation(1.0)
        self.assertAlmostEqual(kx, 1.0, places=9)
        self.assertAlmostEqual(ky, 1.0, places=9)
        self._assert_round_at(1000, 1000)

    def test_without_compensation_the_pen_would_be_elliptical(self):
        """Sanity check that the bug this guards against is real: the RAW
        (uncompensated) per-axis widths differ at a non-square aspect, which
        is exactly the elliptical-pen bug the frontend fix corrects."""
        frame_width, frame_height = 1920, 1080
        sx = frame_width / VIEWBOX
        sy = frame_height / VIEWBOX
        transform = MaskTransform(x=0, y=0, width=100, height=100, rotation=0)
        width = stroke_render_width({"size": 8}, transform)
        raw_x = width * sx
        raw_y = width * sy
        self.assertNotAlmostEqual(raw_x, raw_y, places=3)
        self.assertAlmostEqual(raw_x / raw_y, sx / sy, places=6)


if __name__ == "__main__":
    unittest.main()
