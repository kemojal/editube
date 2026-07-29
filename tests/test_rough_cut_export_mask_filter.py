"""Regression coverage for the masked-export ffmpeg filter graph.

Critical bug (caught by running the real pipeline against a synthetic
video, not by any of the 381 unit tests that existed before this): the
`color` source in the masked export's `-filter_complex` was given its size
in the SAME colon-separated form as `scale`/`pad` ("1920:1080"), but
ffmpeg's `color` filter requires "WxH" ("1920x1080") and silently
mis-parses the colon form -- every masked export failed outright, and this
was NOT caught by the matte fail-open guard (that only wraps
`render_matte_video`; the ffmpeg invocation itself is outside it, so the
exception propagated and failed the whole export job).

`test_filter_complex_uses_x_separated_size_for_color` is the pure-string
assertion that would have caught this immediately. `LiveFfmpegParseTests`
goes further and actually asks ffmpeg to parse (and briefly run) the graph
on a tiny synthetic input, guarded by a `shutil.which("ffmpeg")` skip so it
degrades gracefully on machines without ffmpeg installed -- because the bug
class here is exactly "the string looked plausible and nobody ran it."
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.jobs.rough_cut_export import _masked_filter_complex


class MaskedFilterComplexStringTests(unittest.TestCase):
    def test_filter_complex_uses_x_separated_size_for_color(self):
        filter_complex = _masked_filter_complex(
            "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
            1920,
            1080,
        )
        self.assertIn("color=black:s=1920x1080", filter_complex)
        # scale/pad are correctly colon-separated -- only color differs.
        self.assertIn("scale=1920:1080", filter_complex)
        self.assertIn("pad=1920:1080", filter_complex)
        # Guard against the exact regression: color must never end up with
        # a bare colon-separated size.
        self.assertNotIn("color=black:s=1920:1080", filter_complex)

    def test_filter_complex_matches_arbitrary_resolution(self):
        filter_complex = _masked_filter_complex("scale=640:360", 640, 360)
        self.assertIn("color=black:s=640x360", filter_complex)
        self.assertNotIn("color=black:s=640:360", filter_complex)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
class LiveFfmpegParseTests(unittest.TestCase):
    """Actually invokes ffmpeg on a synthetic clip so a plausible-looking
    but broken filtergraph string cannot pass silently again."""

    def test_ffmpeg_accepts_the_masked_filter_graph(self):
        filter_complex = _masked_filter_complex("scale=64:64", 64, 64)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.mp4"
            proc = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=size=64x64:duration=0.2:rate=10:color=white",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=size=64x64:duration=0.2:rate=10:color=gray",
                    "-filter_complex",
                    filter_complex,
                    "-map",
                    "[v]",
                    "-frames:v",
                    "1",
                    str(out),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(
                proc.returncode,
                0,
                f"ffmpeg rejected the masked filter graph:\n{proc.stderr[-2000:]}",
            )
            self.assertTrue(out.exists())


if __name__ == "__main__":
    unittest.main()
