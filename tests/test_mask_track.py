import unittest

from app.jobs.mask_track import (
    _box_left_frame,
    bbox_to_transform,
    keyframe_stride,
    transform_to_bbox,
)


class BboxConversionTests(unittest.TestCase):
    SIZE = (1920, 1080)

    def test_transform_to_bbox_centres_on_the_frame(self):
        left, top, width, height = transform_to_bbox(
            {"x": 0, "y": 0, "width": 50, "height": 50}, self.SIZE
        )
        self.assertAlmostEqual(width, 960, places=0)
        self.assertAlmostEqual(height, 540, places=0)
        self.assertAlmostEqual(left, 480, places=0)
        self.assertAlmostEqual(top, 270, places=0)

    def test_round_trips_through_bbox(self):
        original = {"x": 12.5, "y": -8.25, "width": 30, "height": 45}
        restored = bbox_to_transform(transform_to_bbox(original, self.SIZE), self.SIZE)
        for key, value in original.items():
            self.assertAlmostEqual(restored[key], value, places=3)

    def test_offscreen_boxes_survive_the_round_trip(self):
        original = {"x": -80, "y": 60, "width": 20, "height": 20}
        restored = bbox_to_transform(transform_to_bbox(original, self.SIZE), self.SIZE)
        self.assertAlmostEqual(restored["x"], -80, places=3)


class KeyframeStrideTests(unittest.TestCase):
    def test_short_clips_emit_a_keyframe_every_few_frames(self):
        self.assertEqual(keyframe_stride(total_frames=60), 1)

    def test_long_clips_are_capped_at_the_keyframe_budget(self):
        stride = keyframe_stride(total_frames=6000)
        self.assertGreater(stride, 1)
        self.assertLessEqual(6000 / stride, 120)

    def test_stride_is_never_zero(self):
        self.assertGreaterEqual(keyframe_stride(total_frames=0), 1)


class BoxLeftFrameTests(unittest.TestCase):
    """Guards the single condition that stops mask tracking: is the box gone
    from the frame? Assert the requirement (trackable vs. gone), not the
    implementation's arithmetic."""

    SIZE = (1920, 1080)  # (frame_w, frame_h)

    def test_box_fully_inside_the_frame_is_not_left(self):
        self.assertFalse(_box_left_frame((100, 100, 200, 200), self.SIZE))

    def test_box_clipped_at_left_edge_is_still_trackable(self):
        # Subject half out of frame on the left is still trackable.
        self.assertFalse(_box_left_frame((-50, 400, 100, 100), self.SIZE))

    def test_box_clipped_at_right_edge_is_still_trackable(self):
        self.assertFalse(_box_left_frame((1870, 400, 100, 100), self.SIZE))

    def test_box_clipped_at_top_edge_is_still_trackable(self):
        self.assertFalse(_box_left_frame((800, -50, 100, 100), self.SIZE))

    def test_box_clipped_at_bottom_edge_is_still_trackable(self):
        self.assertFalse(_box_left_frame((800, 1030, 100, 100), self.SIZE))

    def test_box_beyond_left_edge_by_more_than_its_own_size_is_gone(self):
        # width=100; left edge at -250 puts the box's right edge at -150,
        # which is more than one box-width (100) past the frame boundary.
        self.assertTrue(_box_left_frame((-250, 400, 100, 100), self.SIZE))

    def test_box_beyond_right_edge_by_more_than_its_own_size_is_gone(self):
        self.assertTrue(_box_left_frame((2070, 400, 100, 100), self.SIZE))

    def test_box_beyond_top_edge_by_more_than_its_own_size_is_gone(self):
        self.assertTrue(_box_left_frame((800, -250, 100, 100), self.SIZE))

    def test_box_beyond_bottom_edge_by_more_than_its_own_size_is_gone(self):
        self.assertTrue(_box_left_frame((800, 1230, 100, 100), self.SIZE))

    def test_exact_boundary_is_not_yet_gone(self):
        # width=100; right edge exactly at -width (-100) means the box's
        # trailing edge is exactly one box-width outside — the boundary
        # itself is still considered trackable, only strictly more is "gone".
        self.assertFalse(_box_left_frame((-200, 400, 100, 100), self.SIZE))

    def test_just_past_the_exact_boundary_is_gone(self):
        self.assertTrue(_box_left_frame((-200.01, 400, 100, 100), self.SIZE))

    def test_zero_size_box_is_treated_as_gone(self):
        # A tracker never legitimately reports a zero-size box for a live
        # target; this only guards against a degenerate/corrupt bbox so the
        # job fails safe (stops) rather than tracking a phantom point.
        self.assertTrue(_box_left_frame((800, 400, 0, 0), self.SIZE))


if __name__ == "__main__":
    unittest.main()
