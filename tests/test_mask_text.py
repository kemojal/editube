"""Cross-language parity for Task 3 (Text mask -- real glyphs), driven by the
shared golden fixture
`editube-frontend/docs/fixtures/mask-text-golden.json`.

Text is the one shape the two renderers rasterise with different engines
(the browser's text engine vs FreeType through Pillow), so what is asserted
here is the LAYOUT both engines are handed: per-line absolute BASELINES,
the baseline-to-baseline step, anchors, and which font file each style
resolves to. If those agree, the only remaining difference is glyph
rasterisation itself (hinting/antialiasing), which no headless test can
compare -- see the task report for the bounded discrepancies.

Vendoring follows `test_mask_geometry.py` / `test_mask_expansion.py`: a copy
lives in `tests/fixtures/` so the suite works in the split-repo deploy, with
a sha256 drift check against the frontend's copy when it is reachable. The
same drift check is applied to the TTFs themselves -- a text mask is only
WYSIWYG if the exporter reads byte-identical font files to the ones the
browser loaded.
"""

import hashlib
import json
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from app.services.mask_geometry import mask_is_inert, mask_polygons
from app.services.mask_matte import render_matte_frame, sanitize_masks
from app.services.mask_text import (
    DEFAULT_MASK_FONT_ID,
    MASK_FONTS,
    TEXT_ASCENT_RATIO,
    TEXT_DESCENT_RATIO,
    mask_font_path,
    mask_text_layout,
    resolve_mask_font,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mask-text-golden.json"
_FRONTEND = Path(__file__).resolve().parents[2] / "editube-frontend"
SOURCE_FIXTURE = _FRONTEND / "docs" / "fixtures" / "mask-text-golden.json"
SOURCE_FONT_DIR = _FRONTEND / "public" / "fonts" / "mask-text"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class VendoredTextFixtureDriftTests(unittest.TestCase):
    def test_vendored_copy_matches_the_frontend_source_when_reachable(self):
        if not SOURCE_FIXTURE.exists():
            raise unittest.SkipTest(
                f"monorepo layout not present ({SOURCE_FIXTURE} not found) -- "
                "drift check only applies when the frontend's fixture is reachable"
            )
        self.assertTrue(FIXTURE.exists(), f"vendored fixture missing at {FIXTURE}")
        self.assertEqual(
            _sha256(FIXTURE),
            _sha256(SOURCE_FIXTURE),
            f"vendored fixture has drifted -- re-copy {SOURCE_FIXTURE} to {FIXTURE}",
        )

    def test_vendored_fonts_are_byte_identical_to_the_frontend_copies(self):
        """A text mask is only WYSIWYG if both sides rasterise the SAME file.
        Two copies of 'Vera' from different releases would render subtly
        different glyph widths and nothing else in the suite would notice."""
        if not SOURCE_FONT_DIR.exists():
            raise unittest.SkipTest("monorepo layout not present -- frontend font copies unreachable")
        for font_id, font in MASK_FONTS.items():
            for style, filename in font["files"].items():
                with self.subTest(font=font_id, style=style):
                    mine = mask_font_path(font_id, style)
                    theirs = SOURCE_FONT_DIR / filename
                    self.assertTrue(mine.exists(), f"missing vendored font {mine}")
                    self.assertTrue(theirs.exists(), f"missing frontend font {theirs}")
                    self.assertEqual(_sha256(mine), _sha256(theirs))


class MaskTextLayoutParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not FIXTURE.exists():
            raise RuntimeError(
                f"vendored golden fixture missing at {FIXTURE}; this repo carries its own "
                "copy so parity testing doesn't depend on the monorepo layout."
            )
        cls.cases = json.loads(FIXTURE.read_text())["cases"]

    def test_fixture_has_a_multiline_non_default_size_case(self):
        self.assertTrue(
            any(
                case["expect"] and len(case["expect"]["lines"]) > 1 and case["expect"]["fontSizeVb"] != 200
                for case in self.cases
            )
        )

    def test_matches_golden_layout(self):
        for case in self.cases:
            with self.subTest(case["name"]):
                layout = mask_text_layout(case["mask"], case["t"], case["frameAspect"])
                want = case["expect"]
                if want is None:
                    self.assertIsNone(layout)
                    continue
                self.assertIsNotNone(layout)
                self.assertEqual(layout.font_id, want["fontId"])
                self.assertEqual(layout.font_fallback, want["fontFallback"])
                self.assertEqual(layout.style_key, want["styleKey"])
                self.assertEqual(MASK_FONTS[layout.font_id]["files"][layout.style_key], want["fontFile"])
                self.assertEqual(layout.underline, want["underline"])
                self.assertAlmostEqual(layout.font_size_vb, want["fontSizeVb"], places=3)
                self.assertAlmostEqual(layout.line_step, want["lineStep"], places=3)
                self.assertAlmostEqual(layout.letter_spacing_vb, want["letterSpacingVb"], places=3)
                self.assertEqual(layout.anchor, want["anchor"])
                self.assertEqual(layout.pillow_anchor, want["pillowAnchor"])
                self.assertEqual(layout.align, want["align"])
                self.assertEqual(layout.align_v, want["alignV"])
                self.assertAlmostEqual(layout.underline_offset_vb, want["underlineOffsetVb"], places=3)
                self.assertAlmostEqual(layout.underline_thickness_vb, want["underlineThicknessVb"], places=3)
                self.assertAlmostEqual(layout.rotation, want["rotation"], places=3)
                self.assertAlmostEqual(layout.centre_x, want["centreX"], places=3)
                self.assertAlmostEqual(layout.centre_y, want["centreY"], places=3)
                self.assertEqual(len(layout.lines), len(want["lines"]))
                for got_line, want_line in zip(layout.lines, want["lines"]):
                    self.assertEqual(got_line.text, want_line["text"])
                    self.assertAlmostEqual(got_line.x, want_line["x"], places=3)
                    self.assertAlmostEqual(got_line.baseline_y, want_line["baselineY"], places=3)


def _text_mask(**overrides):
    mask = {
        "id": "t",
        "shape": "text",
        "enabled": True,
        "op": "add",
        "space": "clip",
        "invert": False,
        "x": 0,
        "y": 0,
        "width": 40,
        "height": 40,
        "rotation": 0,
        "feather": 0,
        "roundness": 0,
        "expansion": 0,
        "text": "Text",
        "fontId": DEFAULT_MASK_FONT_ID,
        "fontSize": 20,
        "bold": False,
        "underline": False,
        "italic": False,
        "letterSpacing": 0,
        "lineSpacing": 120,
        "align": "center",
        "alignV": "middle",
        "zoom": 100,
    }
    mask.update(overrides)
    return mask


class MaskTextLayoutRulesTests(unittest.TestCase):
    def test_line_step_is_our_arithmetic_not_a_font_metric(self):
        layout = mask_text_layout(_text_mask(text="a\nb\nc", fontSize=10, lineSpacing=200), 0, 16 / 9)
        self.assertEqual(layout.font_size_vb, 100)
        self.assertEqual(layout.line_step, 200)
        steps = [b.baseline_y - a.baseline_y for a, b in zip(layout.lines, layout.lines[1:])]
        self.assertEqual(steps, [200, 200])

    def test_block_is_vertically_centred_on_the_box(self):
        layout = mask_text_layout(_text_mask(text="one\ntwo", fontSize=10, lineSpacing=150), 0, 16 / 9)
        top = layout.lines[0].baseline_y - TEXT_ASCENT_RATIO * layout.font_size_vb
        bottom = layout.lines[-1].baseline_y + TEXT_DESCENT_RATIO * layout.font_size_vb
        self.assertAlmostEqual((top + bottom) / 2, layout.centre_y, places=6)

    def test_zoom_rides_the_keyframe_channel(self):
        mask = _text_mask(keyframes={"zoom": [{"t": 0, "v": 50}, {"t": 2, "v": 200}]})
        self.assertAlmostEqual(mask_text_layout(mask, 0, 16 / 9).font_size_vb, 100, places=6)
        self.assertAlmostEqual(mask_text_layout(mask, 1, 16 / 9).font_size_vb, 250, places=6)
        # Clamps at both ends, never extrapolates -- the rule every channel obeys.
        self.assertAlmostEqual(mask_text_layout(mask, 99, 16 / 9).font_size_vb, 400, places=6)

    def test_empty_text_paints_nothing_and_is_inert(self):
        self.assertIsNone(mask_text_layout(_text_mask(text="   "), 0, 16 / 9))
        self.assertTrue(mask_is_inert(_text_mask(text="")))
        self.assertFalse(mask_is_inert(_text_mask(text="hi")))

    def test_text_produces_no_polygons(self):
        self.assertEqual(mask_polygons(_text_mask(), 0, 16 / 9), [])

    def test_crlf_does_not_gain_blank_lines(self):
        self.assertEqual(len(mask_text_layout(_text_mask(text="a\r\nb"), 0, 16 / 9).lines), 2)


class MaskTextFontFallbackTests(unittest.TestCase):
    def test_unknown_font_falls_back_and_reports_it(self):
        font_id, fallback = resolve_mask_font("not-a-font-we-ship")
        self.assertEqual(font_id, DEFAULT_MASK_FONT_ID)
        self.assertTrue(fallback)

    def test_shipped_font_is_not_flagged(self):
        self.assertEqual(resolve_mask_font(DEFAULT_MASK_FONT_ID), (DEFAULT_MASK_FONT_ID, False))
        self.assertEqual(resolve_mask_font(None), (DEFAULT_MASK_FONT_ID, False))

    def test_every_shipped_style_file_exists(self):
        for font_id, font in MASK_FONTS.items():
            for style in font["files"]:
                self.assertTrue(mask_font_path(font_id, style).exists(), f"{font_id}/{style}")

    def test_render_collects_the_fallback_for_the_export_warning(self):
        warnings: set[str] = set()
        render_matte_frame([_text_mask(fontId="nope")], 0, (320, 180), warnings)
        self.assertEqual(warnings, {"nope"})


class MaskTextSanitizeTests(unittest.TestCase):
    def test_defaults_and_clamps_untrusted_text_payloads(self):
        mask = sanitize_masks([{"shape": "text"}])[0]
        self.assertEqual(mask["text"], "Text")
        self.assertEqual(mask["fontId"], DEFAULT_MASK_FONT_ID)
        self.assertEqual(mask["fontSize"], 20)
        self.assertEqual(mask["lineSpacing"], 120)
        self.assertEqual(mask["align"], "center")
        self.assertEqual(mask["alignV"], "middle")
        self.assertEqual(mask["zoom"], 100)

        hostile = sanitize_masks(
            [
                {
                    "shape": "text",
                    "text": "x" * 5000,
                    "fontSize": float("nan"),
                    "letterSpacing": -900,
                    "lineSpacing": 0,
                    "zoom": 10**9,
                    "align": "justify",
                    "alignV": "sideways",
                }
            ]
        )[0]
        self.assertEqual(len(hostile["text"]), 500)
        self.assertEqual(hostile["fontSize"], 20)  # NaN -> default, never propagated
        self.assertEqual(hostile["letterSpacing"], -50)
        self.assertEqual(hostile["lineSpacing"], 10)
        self.assertEqual(hostile["zoom"], 400)
        self.assertEqual(hostile["align"], "center")
        self.assertEqual(hostile["alignV"], "middle")

    def test_non_text_shapes_carry_no_text_fields(self):
        mask = sanitize_masks([{"shape": "circle", "text": "hi", "zoom": 250}])[0]
        self.assertNotIn("text", mask)
        self.assertNotIn("zoom", mask)


class MaskTextRasterTests(unittest.TestCase):
    """Sanity checks on the actual Pillow raster -- the layout tests prove
    the numbers agree; these prove the numbers are USED (ink lands where the
    layout says, and an empty mask never blacks out the frame)."""

    def test_ink_straddles_the_baseline_block(self):
        size = (400, 400)
        matte = render_matte_frame([_text_mask(text="Hg", fontSize=20, align="center")], 0, size)
        cols = matte.getbbox()
        self.assertIsNotNone(cols)
        layout = mask_text_layout(_text_mask(text="Hg", fontSize=20, align="center"), 0, 1.0)
        baseline_px = layout.lines[0].baseline_y * (size[1] / 1000)
        top_px = baseline_px - TEXT_ASCENT_RATIO * layout.font_size_vb * (size[1] / 1000)
        bottom_px = baseline_px + TEXT_DESCENT_RATIO * layout.font_size_vb * (size[1] / 1000)
        # Ink must sit inside the line box our layout reserved, with a couple
        # of pixels of slack for overshoot/antialiasing.
        self.assertGreaterEqual(cols[1], top_px - 3)
        self.assertLessEqual(cols[3], bottom_px + 3)

    def test_empty_text_leaves_the_frame_unmasked(self):
        matte = render_matte_frame([_text_mask(text="")], 0, (64, 64))
        self.assertEqual(matte.getextrema(), (255, 255))

    def test_underline_adds_ink_below_the_baseline(self):
        size = (400, 400)
        plain = render_matte_frame([_text_mask(text="ax")], 0, size)
        underlined = render_matte_frame([_text_mask(text="ax", underline=True)], 0, size)
        self.assertGreater(sum(underlined.histogram()[255:]), sum(plain.histogram()[255:]))
        self.assertGreater(underlined.getbbox()[3], plain.getbbox()[3])

    def test_letter_spacing_widens_the_run(self):
        size = (400, 400)
        tight = render_matte_frame([_text_mask(text="ABC")], 0, size).getbbox()
        loose = render_matte_frame([_text_mask(text="ABC", letterSpacing=30)], 0, size).getbbox()
        self.assertGreater(loose[2] - loose[0], tight[2] - tight[0])

    def test_draw_helper_is_a_no_op_for_blank_text(self):
        image = Image.new("L", (32, 32), 0)
        from app.services.mask_text import render_text_layer

        self.assertIsNone(render_text_layer(ImageDraw.Draw(image), _text_mask(text=" "), 0, 1.0, 1, 1, 255))
        self.assertEqual(image.getextrema(), (0, 0))


if __name__ == "__main__":
    unittest.main()
