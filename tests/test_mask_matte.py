import math
import unittest

from app.services.mask_matte import (
    MAX_MASKS,
    MAX_MATTE_FRAMES,
    MAX_POINTS_PER_STROKE,
    render_matte_frame,
    render_matte_video,
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


def brush(strokes, **overrides):
    mask = {
        "id": "m1",
        "shape": "brush",
        "enabled": True,
        "op": "add",
        "space": "clip",
        "invert": False,
        "x": 0,
        "y": 0,
        "width": 100,
        "height": 100,
        "rotation": 0,
        "feather": 0,
        "roundness": 0,
        "strokes": strokes,
    }
    mask.update(overrides)
    return mask


class RenderMatteVideoFrameCapTests(unittest.TestCase):
    """I8: an unbounded/hostile `duration` must fail loudly rather than
    pinning the worker rasterising frames until the multi-hour job timeout."""

    def test_rejects_duration_that_would_exceed_the_frame_cap(self):
        huge_duration = (MAX_MATTE_FRAMES + 100) / 30.0
        with self.assertRaises(RuntimeError):
            render_matte_video(
                [circle()],
                duration=huge_duration,
                fps=30.0,
                size=(64, 64),
                out_path=None,  # never reached -- the cap check raises first
            )


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

    def test_eraser_stroke_only_erases_itself_not_the_whole_mask(self):
        # I5 regression: one eraser dab must not wipe every other stroke in
        # the same brush mask. A normal stroke on the left, an erase stroke
        # on the right; the normal stroke's paint must survive.
        strokes = [
            {"points": [0.15, 0.5, 0.35, 0.5], "size": 20, "erase": False},
            {"points": [0.65, 0.5, 0.85, 0.5], "size": 20, "erase": True},
        ]
        image = render_matte_frame([brush(strokes)], 0.0, self.SIZE)
        # Centre of the normal stroke: (0.25 * 200, 0.5 * 200) = (50, 100).
        self.assertEqual(image.getpixel((50, 100)), 255)
        # Centre of the erase stroke never painted: still background black.
        self.assertEqual(image.getpixel((150, 100)), 0)

    def test_inert_masks_render_nothing_rather_than_blacking_the_frame(self):
        image = render_matte_frame([{"shape": "brush", "enabled": True, "op": "add"}], 0.0, self.SIZE)
        # No usable mask means "no masking" — a fully white matte, not a black one.
        self.assertEqual(image.getpixel((100, 100)), 255)
        self.assertEqual(image.getpixel((2, 2)), 255)

    def test_keyframes_move_the_matte_over_time(self):
        mask = circle(
            keyframes={
                "x": [{"t": 0, "v": -30}, {"t": 2, "v": 30}],
            }
        )
        start = render_matte_frame([mask], 0.0, self.SIZE)
        end = render_matte_frame([mask], 2.0, self.SIZE)
        self.assertNotEqual(start.tobytes(), end.tobytes())

    def test_legacy_whole_transform_keyframe_array_still_animates(self):
        # Phase 1 drafts (or an old cached export payload) store keyframes as
        # a flat whole-transform array. sanitize_mask must migrate it rather
        # than silently dropping the animation.
        mask = circle(
            keyframes=[
                {"t": 0, "x": -30, "y": 0, "width": 40, "height": 40, "rotation": 0},
                {"t": 2, "x": 30, "y": 0, "width": 40, "height": 40, "rotation": 0},
            ]
        )
        start = render_matte_frame([mask], 0.0, self.SIZE)
        end = render_matte_frame([mask], 2.0, self.SIZE)
        self.assertNotEqual(start.tobytes(), end.tobytes())

    def test_keyed_feather_changes_the_rendered_matte_over_time(self):
        # C1 regression: `feather` is a keyframeable channel but was never
        # sampled at render time (mask.get("feather") read the static base
        # value). A keyed feather must actually change the rasterised edge
        # between two different times.
        mask = circle(feather=0, keyframes={"feather": [{"t": 0, "v": 0}, {"t": 2, "v": 80}]})
        start = render_matte_frame([mask], 0.0, self.SIZE)
        end = render_matte_frame([mask], 2.0, self.SIZE)
        self.assertNotEqual(start.tobytes(), end.tobytes())
        edge_values_end = {end.getpixel((x, 100)) for x in range(0, 200)}
        self.assertTrue(any(0 < value < 255 for value in edge_values_end))

    def test_keyed_expansion_changes_the_rendered_matte_over_time(self):
        # C1 regression: same bug for `expansion`.
        mask = circle(width=50, height=50, expansion=0, keyframes={"expansion": [{"t": 0, "v": 0}, {"t": 2, "v": 60}]})
        start = render_matte_frame([mask], 0.0, self.SIZE)
        end = render_matte_frame([mask], 2.0, self.SIZE)
        self.assertNotEqual(start.tobytes(), end.tobytes())

    def test_keyed_roundness_changes_the_rendered_matte_over_time(self):
        # C1 regression: same bug for `roundness` (rectangle only -- circles
        # have no roundness parameter).
        mask = {
            "id": "m1",
            "shape": "rectangle",
            "enabled": True,
            "op": "add",
            "space": "clip",
            "invert": False,
            "x": 0,
            "y": 0,
            "width": 60,
            "height": 60,
            "rotation": 0,
            "feather": 0,
            "roundness": 0,
            "keyframes": {"roundness": [{"t": 0, "v": 0}, {"t": 2, "v": 80}]},
        }
        start = render_matte_frame([mask], 0.0, self.SIZE)
        end = render_matte_frame([mask], 2.0, self.SIZE)
        self.assertNotEqual(start.tobytes(), end.tobytes())
        # A pixel right against the box's literal top-left corner must go
        # from fully-inside (255, plain rectangle) to cut away (0, rounded
        # corner arc) once roundness kicks in. The rectangle spans
        # x/y in [40, 160]px here (width=height=60% of a 200px frame).
        corner = (41, 41)
        self.assertEqual(start.getpixel(corner), 255)
        self.assertEqual(end.getpixel(corner), 0)


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

    def test_rotation_round_trips_minus_90_not_270(self):
        # -90 and 270 are the same rotation, but the frontend's Rotate
        # slider is min=-180/max=180 and cannot render 270 -- sanitisation
        # must always normalise back to (-180, 180], matching wrapRotation
        # in mask-sanitize.ts.
        self.assertEqual(sanitize_mask(circle(rotation=-90))["rotation"], -90)
        self.assertEqual(sanitize_mask(circle(rotation=270))["rotation"], -90)
        self.assertEqual(sanitize_mask(circle(rotation=720))["rotation"], 0)
        self.assertEqual(sanitize_mask(circle(rotation=-400))["rotation"], -40)

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
            "keyframes": {"x": [{"t": i, "v": 0} for i in range(10000)]},
        }
        cleaned = sanitize_mask(mask)
        self.assertLessEqual(len(cleaned["strokes"]), 200)
        self.assertLessEqual(len(cleaned["keyframes"]["x"]), 200)

    def test_unknown_channel_names_are_dropped(self):
        mask = circle(keyframes={"x": [{"t": 0, "v": 5}], "bogusChannel": [{"t": 0, "v": 1}]})
        cleaned = sanitize_mask(mask)
        self.assertNotIn("bogusChannel", cleaned["keyframes"])

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
