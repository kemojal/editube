"""SAM 2 point-prompt segmentation.

Skipped when torch/sam2 are absent, because they are a ~2GB optional install
that `SEGMENTATION_PROVIDER=http` exists specifically to avoid. The skip must
stay a skip and never a silent pass: a test that quietly succeeds without the
model would let a real regression through.

The frames here are synthetic so the assertions can be exact. The property
being tested is not matte quality, it is that the *prompt* controls the output —
which is the whole difference between this and unpromptable matting.
"""

import unittest

import numpy as np

from app.services.segmentation import sam2_backend

REASON = "torch/sam2 not installed (optional; see requirements-ml.txt)"


def two_subjects():
    frame = np.full((360, 640, 3), (18, 18, 24), dtype=np.uint8)
    frame[100:260, 60:220] = (240, 90, 60)
    frame[100:260, 420:580] = (60, 130, 240)
    return frame


def coverage(mask):
    return (
        (mask[100:260, 60:220] > 127).mean(),
        (mask[100:260, 420:580] > 127).mean(),
    )


class DeviceSelectionTests(unittest.TestCase):
    """Runs without torch — pure policy."""

    def test_explicit_override_wins(self):
        # A GPU box that mis-detects, or a bisect against CPU, both need this.
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"SEGMENTATION_DEVICE": "cpu"}):
            self.assertEqual(sam2_backend.pick_device(), "cpu")

    def test_is_installed_does_not_import_torch(self):
        # The capability handshake calls this on every job. If it imported
        # torch, every server would pay a multi-second import to answer "no".
        import sys

        sys.modules.pop("torch", None)
        sam2_backend.is_installed()
        self.assertNotIn("torch", sys.modules)


@unittest.skipUnless(sam2_backend.is_installed(), REASON)
class PointPromptTests(unittest.TestCase):
    def test_a_click_returns_the_clicked_subject(self):
        mask = sam2_backend.segment_at_points(two_subjects(), [(0.22, 0.5)], [1])
        left, right = coverage(mask)
        self.assertGreater(left, 0.9)
        self.assertLess(right, 0.1)

    def test_a_different_click_returns_a_different_subject(self):
        # The assertion that separates promptable segmentation from matting:
        # same frame, different click, different answer.
        frame = two_subjects()
        left_mask = sam2_backend.segment_at_points(frame, [(0.22, 0.5)], [1])
        right_mask = sam2_backend.segment_at_points(frame, [(0.78, 0.5)], [1])
        self.assertGreater(coverage(left_mask)[0], 0.9)
        self.assertGreater(coverage(right_mask)[1], 0.9)

    def test_two_positive_clicks_include_both(self):
        mask = sam2_backend.segment_at_points(
            two_subjects(), [(0.22, 0.5), (0.78, 0.5)], [1, 1]
        )
        left, right = coverage(mask)
        self.assertGreater(left, 0.9)
        self.assertGreater(right, 0.9)

    def test_a_negative_click_subtracts(self):
        mask = sam2_backend.segment_at_points(
            two_subjects(), [(0.22, 0.5), (0.78, 0.5)], [1, 0]
        )
        left, right = coverage(mask)
        self.assertGreater(left, 0.9)
        self.assertLess(right, 0.1)

    def test_returns_a_full_frame_uint8_matte(self):
        # Shape and dtype are a contract: the caller feeds this straight to
        # ffmpeg's alphamerge, which will not resize or rescale for us.
        frame = two_subjects()
        mask = sam2_backend.segment_at_points(frame, [(0.22, 0.5)], [1])
        self.assertEqual(mask.shape, frame.shape[:2])
        self.assertEqual(mask.dtype, np.uint8)
        self.assertEqual(set(np.unique(mask)) - {0, 255}, set())

    def test_quality_tiers_both_resolve(self):
        # "faster"/"better" are user-facing choices; a typo in either model id
        # would only surface when someone picked that tier in production.
        for quality in ("faster", "better"):
            with self.subTest(quality=quality):
                mask = sam2_backend.segment_at_points(
                    two_subjects(), [(0.22, 0.5)], [1], quality=quality
                )
                self.assertGreater(coverage(mask)[0], 0.9)


if __name__ == "__main__":
    unittest.main()
