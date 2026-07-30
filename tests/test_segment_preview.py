"""Single-frame preview: frame decode and mask encoding.

The SAM-dependent assertions are skipped without torch, but the decode half is
not — `extract_frame` reshapes a raw byte stream by dimensions it fetches
separately, and getting that wrong shears the image rather than raising, which
would surface as "the model segmented the wrong thing".
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.services.segmentation import sam2_backend
from app.services.segmentation.base import SegmentationError
from app.services.segmentation.preview import _probe_size, extract_frame, preview_mask_png

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
WIDTH, HEIGHT = 320, 176


def make_clip(path: Path) -> None:
    """Two solid blocks on a dark field, 2s. Non-square so a width/height swap
    in the reshape cannot pass unnoticed."""
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c=0x121218:s={WIDTH}x{HEIGHT}:d=2:r=10",
            "-f", "lavfi", "-i", "color=c=0xF05A3C:s=80x80:d=2:r=10",
            "-filter_complex", "[0][1]overlay=x=30:y=48",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not on PATH")
class FrameDecodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.clip = Path(cls._tmp.name) / "clip.mp4"
        make_clip(cls.clip)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_probe_reports_the_real_size(self):
        self.assertEqual(_probe_size(str(self.clip)), (WIDTH, HEIGHT))

    def test_frame_decodes_to_the_probed_shape(self):
        frame = extract_frame(str(self.clip), 1.0)
        self.assertEqual(frame.shape, (HEIGHT, WIDTH, 3))
        self.assertEqual(frame.dtype.name, "uint8")

    def test_decoded_frame_is_writable(self):
        # frombuffer over immutable bytes yields a read-only array, and torch
        # warns that tensors sharing non-writable memory have undefined write
        # behaviour. Copying is cheap at one frame.
        self.assertTrue(extract_frame(str(self.clip), 0.5).flags.writeable)

    def test_the_frame_is_the_actual_picture_not_sheared(self):
        # A width/height mix-up still produces a plausible-looking array, so
        # check content: the block is orange-ish, the field is near-black.
        frame = extract_frame(str(self.clip), 1.0)
        block = frame[88, 70]
        field = frame[8, 8]
        self.assertGreater(int(block[0]), 150)
        self.assertGreater(int(block[0]), int(block[2]))
        self.assertLess(int(field.max()), 70)

    def test_seeking_past_the_end_fails_with_a_message(self):
        with self.assertRaises(SegmentationError):
            extract_frame(str(self.clip), 90.0)

    def test_a_missing_file_fails_with_a_message_not_a_traceback(self):
        with self.assertRaises(SegmentationError):
            extract_frame("/nonexistent/clip.mp4", 0.0)

    def test_probe_on_a_non_video_fails_with_a_message(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4") as handle:
            handle.write(b"not a video")
            handle.flush()
            with self.assertRaises(SegmentationError):
                _probe_size(handle.name)


@unittest.skipUnless(
    HAVE_FFMPEG and sam2_backend.is_installed(), "needs ffmpeg and SAM 2"
)
class PreviewMaskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.clip = Path(cls._tmp.name) / "clip.mp4"
        make_clip(cls.clip)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_returns_a_greyscale_png_matching_the_frame_size(self):
        import cv2
        import numpy as np

        png, width, height = preview_mask_png(str(self.clip), 1.0, [(0.22, 0.72)], [1])
        self.assertEqual((width, height), (WIDTH, HEIGHT))
        self.assertTrue(png.startswith(b"\x89PNG"))

        decoded = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_UNCHANGED)
        # Greyscale, not RGBA: the client tints it with a theme token, so a
        # colour image here would bake in a colour that is wrong in one theme.
        self.assertEqual(decoded.ndim, 2)
        self.assertEqual(decoded.shape, (HEIGHT, WIDTH))

    def test_the_mask_covers_the_clicked_block(self):
        import cv2
        import numpy as np

        png, _, _ = preview_mask_png(str(self.clip), 1.0, [(0.22, 0.72)], [1])
        mask = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)
        inside = (mask[48:128, 30:110] > 127).mean()
        outside = (mask[0:30, 0:30] > 127).mean()
        self.assertGreater(inside, 0.85)
        self.assertLess(outside, 0.15)

    def test_no_prompt_is_refused_rather_than_guessed_at(self):
        with self.assertRaises(SegmentationError):
            preview_mask_png(str(self.clip), 1.0, [], [])


if __name__ == "__main__":
    unittest.main()
