from __future__ import annotations

import subprocess
import unittest

from app.services.color_adjust_keyframes import build_keyframed_adjust_filter_chain


class ColorAdjustKeyframeTests(unittest.TestCase):
    def test_static_settings_keep_the_normal_compact_chain(self):
        chain = build_keyframed_adjust_filter_chain({"contrast": 20}, 2)
        self.assertEqual(len(chain), 1)
        self.assertIn("eq=", chain[0])
        self.assertNotIn("enable=", chain[0])

    def test_adjust_tracks_are_sampled_and_unrelated_tracks_are_ignored(self):
        chain = build_keyframed_adjust_filter_chain({
            "contrast": 0,
            "hsl": {"red": {"saturation": 0}},
            "keyframes": {
                "adjust.contrast": [{"t": 0, "v": -20}, {"t": 0.3, "v": 30, "easing": "ease-in-out"}],
                "adjust.hsl.red.saturation": [{"t": 0, "v": 0}, {"t": 0.3, "v": 40}],
                "video.x": [{"t": 0, "v": 999}],
            },
        }, 0.3)
        rendered = ",".join(chain)
        self.assertIn("enable=", rendered)
        self.assertIn("huesaturation=", rendered)
        self.assertNotIn("999", rendered)

    def test_ffmpeg_accepts_mixed_keyframed_color_families(self):
        chain = build_keyframed_adjust_filter_chain({
            "temp": 0,
            "contrast": 0,
            "hsl": {"blue": {"hue": 0}},
            "wheels": {"shadows": {"hue": 210, "saturation": 0, "luminance": 0}},
            "keyframes": {
                "adjust.temp": [{"t": 0, "v": -10}, {"t": 0.25, "v": 20}],
                "adjust.contrast": [{"t": 0, "v": 0}, {"t": 0.25, "v": 25}],
                "adjust.hsl.blue.hue": [{"t": 0, "v": -10}, {"t": 0.25, "v": 10}],
                "adjust.wheels.shadows.saturation": [{"t": 0, "v": 0}, {"t": 0.25, "v": 20}],
            },
        }, 0.25)
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=24:duration=0.25",
                "-vf", ",".join(chain), "-frames:v", "6", "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_generated_filter_graph_stays_bounded_for_long_dense_grades(self):
        chain = build_keyframed_adjust_filter_chain({
            "temp": 20,
            "contrast": 15,
            "exposure": 12,
            "hsl": {name: {"hue": 8, "saturation": 12, "brightness": 4} for name in (
                "red", "orange", "yellow", "green", "cyan", "blue", "violet", "magenta"
            )},
            "wheels": {"shadows": {"hue": 210, "saturation": 20, "luminance": -5}},
            "keyframes": {
                "adjust.contrast": [{"t": 0, "v": -20}, {"t": 120, "v": 40}],
                "adjust.hsl.red.hue": [{"t": 0, "v": -10}, {"t": 120, "v": 15}],
            },
        }, 120)
        self.assertLessEqual(len(",".join(chain)), 80_000)


    def test_easing_curves_match_the_shared_fixture(self):
        """`_ease` must trace the same curves the export expressions and the
        editor's `easingRatio` trace — `easing_curves.json` is the contract all
        three assert against, so a curve changed in one place fails everywhere
        else instead of shipping a preview/export drift."""
        import json
        from pathlib import Path

        from app.services.color_adjust_keyframes import _ease

        fixture = json.loads(
            (Path(__file__).parent / "fixtures" / "easing_curves.json").read_text()
        )
        for name, expected in fixture["curves"].items():
            for sample, value in zip(fixture["samples"], expected):
                self.assertAlmostEqual(
                    _ease(sample, name), value, places=9,
                    msg=f"{name} diverges from the fixture at ratio {sample}",
                )


if __name__ == "__main__":
    unittest.main()
