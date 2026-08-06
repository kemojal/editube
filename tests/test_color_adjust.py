from __future__ import annotations

import math
import shutil
import subprocess
import unittest

from app.services.color_adjust import apply_adjust_frame, build_adjust_filter, build_adjust_filter_chain


class ColorAdjustTests(unittest.TestCase):
    def test_disabled_and_neutral_settings_are_inert(self):
        self.assertEqual(build_adjust_filter_chain(None), [])
        self.assertEqual(build_adjust_filter_chain({"enabled": False, "exposure": 100}), [])
        self.assertEqual(build_adjust_filter({"exposure": 0}), "")

    def test_complete_grade_builds_every_filter_family(self):
        rendered = build_adjust_filter({
            "temp": 20,
            "tint": 10,
            "exposure": 12,
            "contrast": 14,
            "saturation": 8,
            "vibrance": 18,
            "highlight": -20,
            "shadow": 15,
            "whites": 8,
            "blacks": -6,
            "brilliance": 10,
            "hsl": {"orange": {"hue": -5, "saturation": 8, "brightness": 3}},
            "curves": {"red": [{"x": 0, "y": 0}, {"x": 0.5, "y": 0.55}, {"x": 1, "y": 1}]},
            "wheels": {"shadows": {"hue": 210, "saturation": 15, "luminance": -4}},
            "fade": 5,
            "sharpen": 10,
            "vignette": 12,
            "grain": 8,
        })
        for expected in ("colortemperature=", "colorbalance=", "exposure=", "eq=", "vibrance=", "huesaturation=", "curves=", "unsharp=", "vignette=", "noise="):
            self.assertIn(expected, rendered)

    def test_nonfinite_and_out_of_range_values_are_safe(self):
        rendered = build_adjust_filter({"exposure": math.inf, "contrast": -99999, "grain": 99999})
        self.assertNotIn("inf", rendered.lower())
        self.assertNotIn("nan", rendered.lower())
        self.assertIn("contrast=0.2000", rendered)
        self.assertIn("alls=24.000", rendered)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
    def test_complex_filter_is_accepted_by_ffmpeg(self):
        grade = build_adjust_filter({
            "temp": 25, "tint": 10, "exposure": 12, "contrast": 14,
            "saturation": 8, "vibrance": 18, "highlight": -20,
            "shadow": 15, "whites": 8, "blacks": -6, "brilliance": 10,
            "hsl": {"red": {"hue": 12, "saturation": 20, "brightness": 5}, "orange": {"hue": -5, "saturation": 8}},
            "curves": {"master": [{"x": 0, "y": 0}, {"x": .25, "y": .2}, {"x": .5, "y": .54}, {"x": .75, "y": .81}, {"x": 1, "y": 1}]},
            "wheels": {"shadows": {"hue": 210, "saturation": 15, "luminance": -4}, "highlights": {"hue": 45, "saturation": 12, "luminance": 5}},
            "fade": 5, "sharpen": 10, "vignette": 12, "grain": 8,
        })
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=0x806040:s=320x180:d=0.1", "-vf", grade, "-frames:v", "1", "-f", "null", "-"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
    def test_exact_frame_preview_uses_the_export_filter_chain(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy is required")
        frame = np.full((48, 64, 3), (60, 90, 130), dtype=np.uint8)
        neutral = apply_adjust_frame(frame, {"enabled": False, "exposure": 50})
        graded = apply_adjust_frame(frame, {"exposure": 25, "contrast": 18, "temp": 10})
        self.assertTrue(np.array_equal(neutral, frame))
        self.assertEqual(graded.shape, frame.shape)
        self.assertFalse(np.array_equal(graded, frame))


if __name__ == "__main__":
    unittest.main()
