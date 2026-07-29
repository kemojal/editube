import math
import unittest

from app.services.mask_matte import (
    MAX_MASKS,
    MAX_POINTS_PER_STROKE,
    render_matte_frame,
    sanitize_mask,
    sanitize_masks,
)


def circle(**overrides):
    mask = {
        "id": "m1",
        "shape": "circle",
        "enabled": True,
        "op": "add",
        "space": "clip",
        "invert": False,
        "x": 0,
        "y": 0,
        "width": 50,
        "height": 50,
        "rotation": 0,
        "feather": 0,
        "roundness": 0,
    }
    mask.update(overrides)
    return mask


class RenderMatteFrameTests(unittest.TestCase):
    SIZE = (200, 200)

    def test_returns_a_grayscale_image_of_the_requested_size(self):
        image = render_matte_frame([circle()], 0.0, self.SIZE)
        self.assertEqual(image.mode, "L")
        self.assertEqual(image.size, self.SIZE)

    def test_centre_is_opaque_and_corner_is_transparent(self):
        image = render_matte_frame([circle()], 0.0, self.SIZE)
        self.assertEqual(image.getpixel((100, 100)), 255)
        self.assertEqual(image.getpixel((2, 2)), 0)

    def test_invert_flips_the_matte(self):
        image = render_matte_frame([circle(invert=True)], 0.0, self.SIZE)
        self.assertEqual(image.getpixel((100, 100)), 0)
        self.assertEqual(image.getpixel((2, 2)), 255)

    def test_subtract_punches_a_hole_in_an_earlier_mask(self):
        masks = [circle(width=80, height=80), circle(id="m2", op="subtract", width=20, height=20)]
        image = render_matte_frame(masks, 0.0, self.SIZE)
        self.assertEqual(image.getpixel((100, 100)), 0)
        # Still inside the big circle, outside the small one.
        self.assertEqual(image.getpixel((100, 135)), 255)

    def test_intersect_keeps_only_the_overlap(self):
        masks = [circle(x=-10, width=50, height=50), circle(id="m2", op="intersect", x=10, width=50, height=50)]
        image = render_matte_frame(masks, 0.0, self.SIZE)
        self.assertEqual(image.getpixel((100, 100)), 255)
        self.assertEqual(image.getpixel((20, 100)), 0)

    def test_feather_produces_intermediate_values_at_the_edge(self):
        hard = render_matte_frame([circle(feather=0)], 0.0, self.SIZE)
        soft = render_matte_frame([circle(feather=40)], 0.0, self.SIZE)
        edge_values = {soft.getpixel((x, 100)) for x in range(40, 160)}
        self.assertTrue(any(0 < value < 255 for value in edge_values))
        self.assertNotEqual(hard.tobytes(), soft.tobytes())

    def test_inert_masks_render_nothing_rather_than_blacking_the_frame(self):
        image = render_matte_frame([{"shape": "brush", "enabled": True, "op": "add"}], 0.0, self.SIZE)
        # No usable mask means "no masking" — a fully white matte, not a black one.
        self.assertEqual(image.getpixel((100, 100)), 255)
        self.assertEqual(image.getpixel((2, 2)), 255)

    def test_keyframes_move_the_matte_over_time(self):
        mask = circle(
            keyframes=[
                {"t": 0, "x": -30, "y": 0, "width": 40, "height": 40, "rotation": 0},
                {"t": 2, "x": 30, "y": 0, "width": 40, "height": 40, "rotation": 0},
            ]
        )
        start = render_matte_frame([mask], 0.0, self.SIZE)
        end = render_matte_frame([mask], 2.0, self.SIZE)
        self.assertNotEqual(start.tobytes(), end.tobytes())


class SanitizeMasksHostileInputTests(unittest.TestCase):
    """Mirrors the frontend's `sanitizeMasks` limits so a malformed or
    hostile payload can never reach Pillow / an ffmpeg subprocess unbounded.
    """

    def test_non_list_input_returns_empty(self):
        self.assertEqual(sanitize_masks(None), [])
        self.assertEqual(sanitize_masks("not a list"), [])
        self.assertEqual(sanitize_masks({"shape": "circle"}), [])

    def test_non_dict_items_are_dropped(self):
        cleaned = sanitize_masks([circle(), "garbage", None, 42, ["nested"]])
        self.assertEqual(len(cleaned), 1)

    def test_caps_at_max_masks(self):
        many = [circle(id=f"m{i}") for i in range(MAX_MASKS + 50)]
        cleaned = sanitize_masks(many)
        self.assertEqual(len(cleaned), MAX_MASKS)

    def test_non_finite_numerics_are_clamped_to_a_default(self):
        cleaned = sanitize_mask(circle(x=float("nan"), y=float("inf"), width=float("-inf")))
        self.assertTrue(all(math.isfinite(v) for v in (cleaned["x"], cleaned["y"], cleaned["width"])))

    def test_non_numeric_numerics_do_not_raise(self):
        cleaned = sanitize_mask(circle(x="not a number", width={"evil": True}, height=["also evil"]))
        self.assertTrue(math.isfinite(cleaned["x"]))
        self.assertTrue(math.isfinite(cleaned["width"]))
        self.assertTrue(math.isfinite(cleaned["height"]))

    def test_unknown_op_and_shape_fall_back_to_safe_defaults(self):
        cleaned = sanitize_mask(circle(op="; DROP TABLE videos;--", shape="<script>alert(1)</script>"))
        self.assertEqual(cleaned["op"], "add")
        self.assertEqual(cleaned["shape"], "rectangle")

    def test_huge_brush_point_arrays_are_capped(self):
        huge_points = [1.0] * (MAX_POINTS_PER_STROKE * 100)
        mask = {
            "shape": "brush",
            "enabled": True,
            "op": "add",
            "strokes": [{"points": huge_points, "size": 4}],
        }
        cleaned = sanitize_mask(mask)
        self.assertLessEqual(len(cleaned["strokes"][0]["points"]), MAX_POINTS_PER_STROKE)

    def test_huge_stroke_and_keyframe_counts_are_capped(self):
        mask = {
            "shape": "brush",
            "enabled": True,
            "op": "add",
            "strokes": [{"points": [0, 0, 1, 1, 2, 2], "size": 4} for _ in range(5000)],
            "keyframes": [{"t": i, "x": 0, "y": 0, "width": 10, "height": 10, "rotation": 0} for i in range(10000)],
        }
        cleaned = sanitize_mask(mask)
        self.assertLessEqual(len(cleaned["strokes"]), 200)
        self.assertLessEqual(len(cleaned["keyframes"]), 200)

    def test_hostile_payload_never_raises_and_stays_bounded(self):
        hostile = [
            {"shape": "pen", "enabled": True, "op": "add", "path": {"points": [{"x": "boom"} for _ in range(100000)]}},
            {"shape": "brush", "enabled": True, "op": "subtract", "strokes": "not-a-list"},
            {"width": float("nan"), "height": float("nan"), "rotation": float("inf")},
        ] * 20
        # Must not raise, and must not blow past MAX_MASKS regardless of
        # how many hostile entries were sent.
        cleaned = sanitize_masks(hostile)
        self.assertLessEqual(len(cleaned), MAX_MASKS)
        # And it must still be safe to rasterise.
        image = render_matte_frame(hostile, 0.0, (64, 64))
        self.assertEqual(image.size, (64, 64))


if __name__ == "__main__":
    unittest.main()
