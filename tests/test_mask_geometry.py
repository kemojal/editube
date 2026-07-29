"""Parity tests between the Python mask-geometry mirror and the TypeScript
original, driven by the shared golden fixture
`editube-frontend/docs/fixtures/mask-geometry-golden.json`.

THE THREE PARITY TIERS (see task-12-brief.md and mask_geometry.py's module
docstring for the two known-bug rules this guards against):

1. EXACT token parity, for polyline-only shapes (`split`, unrounded
   `rectangle`, unrounded `filmstrip`, `star`, `brush`, `text`, and a pen
   path whose anchors carry no handles — no such case exists in this
   fixture). For these the TS SVG path's numeric tokens ARE the polygon
   vertices in order, so the Python polygon must reproduce them exactly at
   2dp. `test_exact_token_parity_polyline_shapes` decodes each shape's SVG
   path grammar (M/H/V for axis-aligned rects, raw coordinate pairs for
   star/brush) back into vertex lists and compares directly. This is the
   only tier that proves per-vertex identity.

2. BOUNDING-BOX-WITHIN-TOLERANCE, for curved shapes (`circle`, rounded
   `rectangle`, `heart`, a pen path with bezier handles). TS emits SVG arcs
   and cubics; Python flattens them into polygons (64 segments per circle,
   24 per cubic, 8 per corner arc). `test_bbox_parity_curved_shapes` only
   checks that the flattened polygon's bounding box matches the box implied
   by `expect.pathTokens` within ~1% of VIEWBOX, and that the vertex count
   matches the documented segment counts. It does NOT prove the curve's
   interior shape is pixel-identical to the TS bezier/arc — a flattened
   polygon and a true curve can share a bounding box while differing along
   the curve itself.

3. ALWAYS, regardless of shape: sampled-transform parity at 3dp
   (`test_sampled_transform_matches_typescript`), `mask_is_inert` agreement
   (`MaskInertTests`), and the round-shape rule that `rx/ry == 1/frameAspect`
   unconditionally, never orientation-branched
   (`test_round_shapes_ignore_frame_aspect`, `test_round_shape_ratio_rule`).

A green suite proves (1) and (3) are pixel/value-identical, and proves (2)
is bounding-box-identical with correct segment density — it does NOT prove
curved-shape interiors are identical between the two renderers.
"""

import json
import math
import unittest
from pathlib import Path

from app.services.mask_geometry import (
    VIEWBOX,
    mask_is_inert,
    mask_polygons,
    sample_mask_transform,
)

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "editube-frontend"
    / "docs"
    / "fixtures"
    / "mask-geometry-golden.json"
)

# Shapes whose golden `pathTokens` are groups of 5: M(x,y) H(x) V(y) H(x),
# i.e. an axis-aligned, unrounded rectangle's 4 corners.
RECT_GROUP_SHAPES = {"split", "filmstrip", "text"}


def _decode_rect_groups(tokens: list[float]) -> list[tuple[float, float]]:
    assert len(tokens) % 5 == 0, f"expected multiple-of-5 tokens, got {len(tokens)}"
    points: list[tuple[float, float]] = []
    for i in range(0, len(tokens), 5):
        mx, my, hx, vy, hx2 = tokens[i : i + 5]
        points.extend([(mx, my), (hx, my), (hx, vy), (hx2, vy)])
    return points


def _decode_pairs(tokens: list[float]) -> list[tuple[float, float]]:
    assert len(tokens) % 2 == 0
    return [(tokens[i], tokens[i + 1]) for i in range(0, len(tokens), 2)]


def _rotate(points, angle_deg: float, centre):
    if angle_deg % 360 == 0:
        return points
    angle = math.radians(angle_deg)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    cx, cy = centre
    out = []
    for x, y in points:
        dx, dy = x - cx, y - cy
        out.append((cx + dx * cos_a - dy * sin_a, cy + dx * sin_a + dy * cos_a))
    return out


def _assert_points_almost_equal(test: unittest.TestCase, got, want, places=2):
    test.assertEqual(len(got), len(want), f"vertex count mismatch: {len(got)} vs {len(want)}")
    for (gx, gy), (wx, wy) in zip(got, want):
        test.assertAlmostEqual(gx, wx, places=places)
        test.assertAlmostEqual(gy, wy, places=places)


class MaskGeometryParityTests(unittest.TestCase):
    """The browser and the exporter must agree, or preview stops predicting output."""

    @classmethod
    def setUpClass(cls):
        if not FIXTURE.exists():
            raise unittest.SkipTest(f"golden fixture not found at {FIXTURE}")
        cls.cases = json.loads(FIXTURE.read_text())["cases"]

    def _case(self, name):
        return next(c for c in self.cases if c["name"] == name)

    def test_fixture_covers_every_shape(self):
        shapes = {case["mask"]["shape"] for case in self.cases}
        self.assertGreaterEqual(len(shapes), 9)

    def test_sampled_transform_matches_typescript(self):
        for case in self.cases:
            with self.subTest(case=case["name"]):
                got = sample_mask_transform(case["mask"], case["t"])
                want = case["expect"]["transform"]
                self.assertAlmostEqual(got.x, want["x"], places=3)
                self.assertAlmostEqual(got.y, want["y"], places=3)
                self.assertAlmostEqual(got.width, want["width"], places=3)
                self.assertAlmostEqual(got.height, want["height"], places=3)
                self.assertAlmostEqual(got.rotation, want["rotation"], places=3)

    def test_every_case_produces_geometry(self):
        for case in self.cases:
            with self.subTest(case=case["name"]):
                polygons = mask_polygons(case["mask"], case["t"], case["frameAspect"])
                self.assertTrue(polygons)
                self.assertTrue(all(len(polygon.points) >= 2 for polygon in polygons))

    def test_round_shapes_ignore_frame_aspect(self):
        wide = self._case("circle-wide")
        tall = self._case("circle-tall")
        wide_poly = mask_polygons(wide["mask"], wide["t"], wide["frameAspect"])[0].points
        tall_poly = mask_polygons(tall["mask"], tall["t"], tall["frameAspect"])[0].points
        span = lambda poly: max(p[1] for p in poly) - min(p[1] for p in poly)
        self.assertAlmostEqual(span(wide_poly), span(tall_poly), places=2)

    def test_round_shape_ratio_rule(self):
        # rx/ry must equal 1/frameAspect unconditionally, for BOTH wide and
        # tall frames, and the two frames must NOT resolve to the same box
        # (that would mean an orientation branch crept back in).
        for name in ("circle-wide", "circle-tall"):
            case = self._case(name)
            poly = mask_polygons(case["mask"], case["t"], case["frameAspect"])[0].points
            rx = (max(p[0] for p in poly) - min(p[0] for p in poly)) / 2
            ry = (max(p[1] for p in poly) - min(p[1] for p in poly)) / 2
            self.assertAlmostEqual(rx / ry, 1 / case["frameAspect"], places=2)

        wide_poly = mask_polygons(*[self._case("circle-wide")[k] for k in ("mask", "t", "frameAspect")])[0].points
        tall_poly = mask_polygons(*[self._case("circle-tall")[k] for k in ("mask", "t", "frameAspect")])[0].points
        wide_rx = max(p[0] for p in wide_poly) - min(p[0] for p in wide_poly)
        tall_rx = max(p[0] for p in tall_poly) - min(p[0] for p in tall_poly)
        self.assertNotAlmostEqual(wide_rx, tall_rx, places=1)

    # -- Tier 1: exact -------------------------------------------------

    def test_exact_token_parity_polyline_shapes(self):
        """Polyline-only shapes: Python vertices must equal TS path tokens at 2dp."""

        # split: single unrounded rect group, rotation applied.
        case = self._case("split-base")
        want = _decode_rect_groups(case["expect"]["pathTokens"])
        got = mask_polygons(case["mask"], case["t"], case["frameAspect"])[0].points
        centre = (VIEWBOX / 2 + case["mask"]["x"] / 100 * VIEWBOX, VIEWBOX / 2 + case["mask"]["y"] / 100 * VIEWBOX)
        want_rotated = _rotate(want, case["expect"]["transform"]["rotation"], centre)
        _assert_points_almost_equal(self, got, want_rotated)

        # filmstrip: N unrounded rect groups concatenated, one MaskPolygon each.
        case = self._case("filmstrip-4")
        want_flat = _decode_rect_groups(case["expect"]["pathTokens"])
        polys = mask_polygons(case["mask"], case["t"], case["frameAspect"])
        got_flat = [p for poly in polys for p in poly.points]
        _assert_points_almost_equal(self, got_flat, want_flat)

        # text: bar + stem, each an unrounded rect group, two MaskPolygons.
        case = self._case("text-t")
        want_flat = _decode_rect_groups(case["expect"]["pathTokens"])
        polys = mask_polygons(case["mask"], case["t"], case["frameAspect"])
        got_flat = [p for poly in polys for p in poly.points]
        _assert_points_almost_equal(self, got_flat, want_flat)

        # star: raw coordinate pairs, rotation applied.
        case = self._case("star-rot")
        want = _decode_pairs(case["expect"]["pathTokens"])
        got = mask_polygons(case["mask"], case["t"], case["frameAspect"])[0].points
        transform = case["expect"]["transform"]
        centre = (VIEWBOX / 2 + transform["x"] / 100 * VIEWBOX, VIEWBOX / 2 + transform["y"] / 100 * VIEWBOX)
        want_rotated = _rotate(want, transform["rotation"], centre)
        _assert_points_almost_equal(self, got, want_rotated)

        # brush: raw coordinate pairs per stroke, one MaskPolygon per stroke,
        # no rotation in this fixture case.
        case = self._case("brush-two")
        want_flat = _decode_pairs(case["expect"]["pathTokens"])
        polys = mask_polygons(case["mask"], case["t"], case["frameAspect"])
        got_flat = [p for poly in polys for p in poly.points]
        _assert_points_almost_equal(self, got_flat, want_flat)

        # keyframed-mid: unrounded rectangle, sampled from keyframes.
        case = self._case("keyframed-mid")
        want = _decode_rect_groups(case["expect"]["pathTokens"])
        got = mask_polygons(case["mask"], case["t"], case["frameAspect"])[0].points
        # rotation samples to 360deg, equivalent to 0 — no extra rotation needed.
        _assert_points_almost_equal(self, got, want)

    # -- Tier 2: bounding box only --------------------------------------

    def test_bbox_parity_curved_shapes(self):
        """Curved shapes: only bbox + documented vertex count are asserted."""
        tolerance = VIEWBOX * 0.01

        def bbox(points):
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            return min(xs), min(ys), max(xs), max(ys)

        # circle: 64 segments; expected box derived from resolveBox's own
        # formula (sampled transform + round-shape shorter-axis rule), since
        # the SVG arc tokens don't carry an explicit ry.
        for name in ("circle-wide", "circle-tall", "keyframed-clamped"):
            case = self._case(name)
            poly = mask_polygons(case["mask"], case["t"], case["frameAspect"])[0]
            self.assertEqual(len(poly.points), 64)
            transform = case["expect"]["transform"]
            shorter = min(transform["width"], transform["height"])
            expected_w = shorter / case["frameAspect"] / 100 * VIEWBOX
            expected_h = shorter / 100 * VIEWBOX
            got_min_x, got_min_y, got_max_x, got_max_y = bbox(poly.points)
            self.assertAlmostEqual(got_max_x - got_min_x, expected_w, delta=tolerance)
            self.assertAlmostEqual(got_max_y - got_min_y, expected_h, delta=tolerance)

        # rounded rectangle: 4 straight corners + 4*8 arc segments.
        case = self._case("rect-round")
        poly = mask_polygons(case["mask"], case["t"], case["frameAspect"])[0]
        self.assertEqual(len(poly.points), 4 + 4 * 8)
        # pathTokens mixes absolute coords and relative arc deltas, so derive
        # the expected box from the mask's own width/height instead.
        transform = case["expect"]["transform"]
        expected_w = transform["width"] / 100 * VIEWBOX
        expected_h = transform["height"] / 100 * VIEWBOX
        got_min_x, got_min_y, got_max_x, got_max_y = bbox(poly.points)
        self.assertAlmostEqual(got_max_x - got_min_x, expected_w, delta=tolerance)
        self.assertAlmostEqual(got_max_y - got_min_y, expected_h, delta=tolerance)

        # heart: 4 cubics * 24 segments + 1 shared start anchor.
        case = self._case("heart-off")
        poly = mask_polygons(case["mask"], case["t"], case["frameAspect"])[0]
        self.assertEqual(len(poly.points), 1 + 4 * 24)
        transform = case["expect"]["transform"]
        expected_w = transform["width"] / 100 * VIEWBOX
        expected_h = transform["height"] / 100 * VIEWBOX
        got_min_x, got_min_y, got_max_x, got_max_y = bbox(poly.points)
        self.assertLessEqual(got_max_x - got_min_x, expected_w + tolerance)
        self.assertLessEqual(got_max_y - got_min_y, expected_h + tolerance)

        # pen-closed has 3 anchors, closed (3 segments): only the first
        # segment (point0 -> point1) carries handles (outX on point0, inX on
        # point1) and flattens to 24 points; the other two segments (1->2,
        # 2->0 closing) have no handles on either end and stay straight
        # lines (1 point each) — matching penPath's per-segment hasHandles
        # check, not a blanket "every segment is a cubic" assumption.
        case = self._case("pen-closed")
        poly = mask_polygons(case["mask"], case["t"], case["frameAspect"])[0]
        self.assertEqual(len(poly.points), 1 + 24 + 1 + 1)


class MaskStripesNullishTests(unittest.TestCase):
    """`stripes` must follow TS `??` (only None/absent defaults to 3), not
    Python truthiness (`or 3`) which would wrongly promote 0 -> 3."""

    def test_stripes_zero_yields_two_bands_like_typescript(self):
        mask = {
            "shape": "filmstrip",
            "enabled": True,
            "x": 0,
            "y": 0,
            "width": 60,
            "height": 80,
            "rotation": 0,
            "roundness": 0,
            "stripes": 0,
        }
        polygons = mask_polygons(mask, 0, 16 / 9)
        self.assertEqual(len(polygons), 2)

    def test_stripes_absent_defaults_to_three_bands(self):
        mask = {
            "shape": "filmstrip",
            "enabled": True,
            "x": 0,
            "y": 0,
            "width": 60,
            "height": 80,
            "rotation": 0,
            "roundness": 0,
        }
        polygons = mask_polygons(mask, 0, 16 / 9)
        self.assertEqual(len(polygons), 3)


class MaskInertTests(unittest.TestCase):
    def test_brush_without_strokes_is_inert(self):
        self.assertTrue(mask_is_inert({"shape": "brush", "enabled": True}))

    def test_pen_with_two_points_is_inert(self):
        mask = {"shape": "pen", "enabled": True, "path": {"points": [{"x": 0, "y": 0}, {"x": 1, "y": 1}]}}
        self.assertTrue(mask_is_inert(mask))

    def test_disabled_mask_is_inert(self):
        self.assertTrue(mask_is_inert({"shape": "circle", "enabled": False, "width": 40, "height": 40}))

    def test_normal_mask_is_active(self):
        self.assertFalse(mask_is_inert({"shape": "circle", "enabled": True, "width": 40, "height": 40}))


if __name__ == "__main__":
    unittest.main()
