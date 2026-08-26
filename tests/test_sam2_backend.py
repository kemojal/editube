"""SAM 2 point-prompt segmentation.

Skipped when torch/sam2 are absent, because they are a ~2GB optional install
that `SEGMENTATION_PROVIDER=http` exists specifically to avoid. The skip must
stay a skip and never a silent pass: a test that quietly succeeds without the
model would let a real regression through.

The frames here are synthetic so the assertions can be exact. The property
being tested is not matte quality, it is that the *prompt* controls the output —
which is the whole difference between this and unpromptable matting.
"""

import threading
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

    def test_additive_groups_preserve_the_existing_subject(self):
        mask = sam2_backend.segment_prompt_groups(
            two_subjects(),
            [[(0.22, 0.5)], [(0.78, 0.5)]],
        )
        left, right = coverage(mask)
        self.assertGreater(left, 0.9)
        self.assertGreater(right, 0.9)

    def test_negative_context_is_applied_to_every_additive_group(self):
        mask = sam2_backend.segment_prompt_groups(
            two_subjects(),
            [[(0.22, 0.5)]],
            [(0.78, 0.5)],
        )
        left, right = coverage(mask)
        self.assertGreater(left, 0.9)
        self.assertLess(right, 0.1)

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


@unittest.skipUnless(sam2_backend.is_installed(), REASON)
class ConcurrencyTests(unittest.TestCase):
    """Two clicks at once must not break the process or cross their answers.

    This is a regression test for a real failure, not a hypothetical. The
    interactive preview runs in the API process, so two clicks land on two
    threads, and the original code shared one predictor behind an lru_cache with
    no lock. Two consequences, both observed:

      * `lru_cache` does not hold a lock across the call, so both threads ran
        `SAM2ImagePredictor.from_pretrained`, which calls `torch.jit.script`.
        That is not re-entrant, and it corrupted the JIT compilation unit
        *permanently* — every subsequent request in the process failed with
        "Can't redefine method: forward on class: ...transforms.Resize".
      * With construction serialised, concurrent *inference* then wedged one
        thread inside MPS `layer_norm` indefinitely.

    Both only appear on a cold cache, which is why a warm two-thread check
    passed and this reached production.
    """

    def setUp(self):
        # Cold, which is the only state that reproduces either failure.
        sam2_backend.clear_predictors()

    def test_concurrent_cold_requests_all_succeed(self):
        errors: list[str] = []
        masks: dict[int, object] = {}

        def work(index):
            try:
                masks[index] = sam2_backend.segment_at_points(
                    two_subjects(), [(0.22, 0.5)], [1]
                )
            except BaseException as exc:  # noqa: BLE001 - recording, not handling
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            # Generous but finite: the bug was an indefinite hang, so a timeout
            # is the assertion. Cold load is a few seconds.
            thread.join(timeout=300)

        self.assertFalse([t for t in threads if t.is_alive()], "a thread hung in inference")
        self.assertEqual(errors, [])
        self.assertEqual(len(masks), 4)

    def test_the_process_still_works_after_concurrent_use(self):
        # The JIT corruption was permanent, so a single call afterwards is the
        # thing that actually proves the process survived.
        def work():
            try:
                sam2_backend.segment_at_points(two_subjects(), [(0.22, 0.5)], [1])
            except BaseException:  # noqa: BLE001
                pass

        threads = [threading.Thread(target=work) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=300)

        mask = sam2_backend.segment_at_points(two_subjects(), [(0.22, 0.5)], [1])
        self.assertGreater(coverage(mask)[0], 0.9)

    def test_concurrent_requests_do_not_cross_their_frames(self):
        # The correctness half: `set_image` stores embeddings on the predictor,
        # so an interleaved set/predict segments the *other* thread's target.
        # Different prompts, so a crossed answer is visible rather than benign.
        outcomes: dict[int, tuple[float, float]] = {}

        def work(index, point):
            outcomes[index] = coverage(
                sam2_backend.segment_at_points(two_subjects(), [point], [1])
            )

        threads = [
            threading.Thread(target=work, args=(i, (0.22, 0.5) if i % 2 == 0 else (0.78, 0.5)))
            for i in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=300)

        self.assertEqual(len(outcomes), 4)
        for index, (left, right) in outcomes.items():
            with self.subTest(thread=index):
                if index % 2 == 0:
                    self.assertGreater(left, 0.9)
                    self.assertLess(right, 0.1)
                else:
                    self.assertGreater(right, 0.9)
                    self.assertLess(left, 0.1)

    def test_a_warm_predictor_is_reused_rather_than_rebuilt(self):
        # Rebuilding per call would be correct but unusably slow, so the cache
        # has to still be a cache after the locking change.
        sam2_backend.segment_at_points(two_subjects(), [(0.22, 0.5)], [1])
        before = len(sam2_backend._PREDICTORS)
        sam2_backend.segment_at_points(two_subjects(), [(0.78, 0.5)], [1])
        self.assertEqual(len(sam2_backend._PREDICTORS), before)
