"""Motion-adaptive segmentation: which frames the model actually runs on.

This policy is the reason background removal went from 34s to 8.4s for 5s of
1080p30 (4.0x, tiny model both sides, 99.93% mean agreement with the every-frame
result). It is also the thing that would silently degrade output if it got too
aggressive, which is why the decision is tested on its own rather than only
through the pipeline.
"""

import unittest

import numpy as np

from app.services.segmentation.matte_stride import (
    MAX_STRIDE,
    StrideDecider,
    blend_mattes,
    motion_between,
    motion_probe,
    stride_for_quality,
)


def flat(value):
    return np.full((180, 320, 3), value, dtype=np.uint8)


def moving(offset):
    frame = np.zeros((180, 320, 3), dtype=np.uint8)
    frame[40:140, offset : offset + 80] = 255
    return frame


class QualityTierTests(unittest.TestCase):
    def test_better_means_every_frame(self):
        # Someone who asked for better quality must not silently get interpolated
        # frames.
        self.assertEqual(stride_for_quality("better"), 1)
        self.assertEqual(stride_for_quality("BETTER"), 1)

    def test_faster_allows_reuse(self):
        self.assertEqual(stride_for_quality("faster"), MAX_STRIDE)

    def test_an_unknown_or_missing_tier_is_treated_as_faster(self):
        for value in ("", None, "medium"):
            with self.subTest(value=value):
                self.assertEqual(stride_for_quality(value), MAX_STRIDE)


class MotionMeasurementTests(unittest.TestCase):
    def test_identical_frames_have_no_motion(self):
        self.assertEqual(motion_between(motion_probe(flat(90)), motion_probe(flat(90))), 0.0)

    def test_a_changed_frame_has_motion(self):
        self.assertGreater(motion_between(motion_probe(flat(0)), motion_probe(flat(255))), 100)

    def test_motion_grows_with_displacement(self):
        base = motion_probe(moving(40))
        near = motion_between(base, motion_probe(moving(44)))
        far = motion_between(base, motion_probe(moving(120)))
        self.assertLess(near, far)

    def test_the_probe_is_small_enough_to_be_free(self):
        # This runs on every frame; if it were expensive it would eat the saving
        # it exists to create.
        self.assertLessEqual(motion_probe(flat(50)).size, 64 * 64)


class StrideDeciderTests(unittest.TestCase):
    def test_the_first_frame_always_runs(self):
        decider = StrideDecider()
        self.assertTrue(decider.needs_segmentation(motion_probe(flat(50))))

    def test_a_static_shot_reuses_up_to_the_cap(self):
        decider = StrideDecider(max_stride=4)
        probe = motion_probe(flat(50))
        decisions = [decider.needs_segmentation(probe) for _ in range(9)]
        # First runs, then reuse until the forced refresh.
        self.assertTrue(decisions[0])
        self.assertGreater(decider.reused, decider.segmented)

    def test_the_cap_is_enforced_so_drift_cannot_accumulate(self):
        # A slow continuous move can stay under the threshold every single frame
        # while ending up somewhere completely different. The cap is what stops
        # that becoming an invisible tracking failure.
        decider = StrideDecider(max_stride=3)
        probe = motion_probe(flat(50))
        gaps, since = [], 0
        for _ in range(12):
            if decider.needs_segmentation(probe):
                gaps.append(since)
                since = 0
            else:
                since += 1
        self.assertTrue(all(gap <= 2 for gap in gaps[1:]), gaps)

    def test_real_motion_always_forces_segmentation(self):
        decider = StrideDecider(max_stride=8)
        decider.needs_segmentation(motion_probe(moving(10)))
        # A big jump must not be interpolated over.
        self.assertTrue(decider.needs_segmentation(motion_probe(moving(200))))

    def test_max_stride_of_one_disables_reuse_entirely(self):
        decider = StrideDecider(max_stride=1)
        probe = motion_probe(flat(50))
        self.assertTrue(all(decider.needs_segmentation(probe) for _ in range(6)))
        self.assertEqual(decider.reused, 0)

    def test_speedup_reports_calls_avoided(self):
        decider = StrideDecider(max_stride=4)
        probe = motion_probe(flat(50))
        for _ in range(8):
            decider.needs_segmentation(probe)
        self.assertGreater(decider.speedup, 1.0)
        self.assertEqual(decider.segmented + decider.reused, 8)

    def test_speedup_is_one_when_nothing_was_reused(self):
        decider = StrideDecider(max_stride=1)
        decider.needs_segmentation(motion_probe(flat(50)))
        self.assertEqual(decider.speedup, 1.0)


class BlendTests(unittest.TestCase):
    def setUp(self):
        self.a = np.zeros((10, 10), dtype=np.uint8)
        self.b = np.full((10, 10), 255, dtype=np.uint8)

    def test_the_endpoints_are_returned_exactly(self):
        np.testing.assert_array_equal(blend_mattes(self.a, self.b, 0.0), self.a)
        np.testing.assert_array_equal(blend_mattes(self.a, self.b, 1.0), self.b)

    def test_the_midpoint_is_halfway(self):
        self.assertAlmostEqual(int(blend_mattes(self.a, self.b, 0.5)[0, 0]), 127, delta=2)

    def test_the_blend_is_monotonic(self):
        values = [int(blend_mattes(self.a, self.b, w)[0, 0]) for w in (0, 0.25, 0.5, 0.75, 1)]
        self.assertEqual(values, sorted(values))

    def test_output_stays_uint8_and_in_range(self):
        result = blend_mattes(self.a, self.b, 0.3)
        self.assertEqual(result.dtype, np.uint8)
        self.assertGreaterEqual(int(result.min()), 0)
        self.assertLessEqual(int(result.max()), 255)

    def test_out_of_range_weights_are_clamped_to_the_endpoints(self):
        np.testing.assert_array_equal(blend_mattes(self.a, self.b, -1), self.a)
        np.testing.assert_array_equal(blend_mattes(self.a, self.b, 5), self.b)


if __name__ == "__main__":
    unittest.main()
