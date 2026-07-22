import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.services.thumbnail import generate_and_store_thumbnail, generate_thumbnail_to_path
from app.storage import get_storage, reset_storage_cache
from app.storage.local import LocalBackend

_HAS_FFMPEG = shutil.which("ffmpeg") is not None


class ThumbnailGracefulTests(unittest.TestCase):
    def test_empty_src_returns_none(self):
        self.assertIsNone(generate_and_store_thumbnail("", folder="thumbnails", public_id="x"))

    def test_bad_path_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp) / "out.jpg"
            self.assertFalse(generate_thumbnail_to_path("/nope/missing.mp4", dst))
            self.assertFalse(dst.exists())


@unittest.skipUnless(_HAS_FFMPEG, "ffmpeg not installed")
class ThumbnailFfmpegTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # a 1s test video via lavfi
        self.video = Path(self.tmp.name) / "sample.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10",
             str(self.video)],
            capture_output=True, check=True,
        )
        self._prev = os.environ.get("STORAGE_BACKEND")
        os.environ["STORAGE_BACKEND"] = "local"
        os.environ["BASE_URL"] = "https://example.test"
        reset_storage_cache()
        # point local backend at the temp dir
        backend = get_storage()
        self.assertIsInstance(backend, LocalBackend)
        backend._root = Path(self.tmp.name).resolve()

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("STORAGE_BACKEND", None)
        else:
            os.environ["STORAGE_BACKEND"] = self._prev
        reset_storage_cache()
        self.tmp.cleanup()

    def test_extract_frame(self):
        dst = Path(self.tmp.name) / "frame.jpg"
        self.assertTrue(generate_thumbnail_to_path(str(self.video), dst, seek=0.0))
        self.assertTrue(dst.stat().st_size > 0)

    def test_generate_and_store(self):
        url = generate_and_store_thumbnail(
            str(self.video), folder="thumbnails", public_id="video_1", seek=0.0
        )
        self.assertIsNotNone(url)
        self.assertEqual(url, "https://example.test/uploads/thumbnails/video_1.jpg")
        self.assertTrue((Path(self.tmp.name) / "thumbnails/video_1.jpg").stat().st_size > 0)


if __name__ == "__main__":
    unittest.main()
