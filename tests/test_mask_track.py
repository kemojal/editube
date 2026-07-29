import unittest

from app.jobs.mask_track import (
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


if __name__ == "__main__":
    unittest.main()
