"""The .cube pipeline: parse, intensity bake, and the ffmpeg chain placement."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from app.services.color_adjust import build_adjust_filter_chain
from app.services.color_adjust_keyframes import build_keyframed_adjust_filter_chain
from app.services.lut import (
    LutError,
    blend_with_identity,
    parse_cube,
    write_cube,
)

#: 2³ identity cube — every lattice point maps to itself.
IDENTITY_2 = """LUT_3D_SIZE 2
0 0 0
1 0 0
0 1 0
1 1 0
0 0 1
1 0 1
0 1 1
1 1 1
"""

#: Warm look: red gain, blue cut.
WARM_2 = """TITLE "warm"
LUT_3D_SIZE 2
0.1 0 0
1 0 0
0.1 1 0
1 1 0
0.1 0 0.8
1 0 0.8
0.1 1 0.8
1 1 0.8
"""


class CubeParserTests(unittest.TestCase):
    def test_parses_identity_and_keeps_file_order(self):
        lut = parse_cube(IDENTITY_2)
        self.assertEqual(lut["size"], 2)
        # Red runs fastest: the second row is the pure-red corner.
        self.assertEqual(lut["table"][1], (1.0, 0.0, 0.0))
        self.assertEqual(lut["table"][7], (1.0, 1.0, 1.0))

    def test_rejects_what_is_not_a_3d_cube(self):
        with self.assertRaises(LutError):
            parse_cube("not a lut at all")
        with self.assertRaises(LutError):
            parse_cube("LUT_1D_SIZE 4\n0\n0.33\n0.66\n1\n")
        with self.assertRaises(LutError):
            parse_cube("LUT_3D_SIZE 2\n0 0 0\n")  # short table
        with self.assertRaises(LutError):
            parse_cube(IDENTITY_2.replace("1 1 1", "1 1 nan"))
        with self.assertRaises(LutError):
            parse_cube("LUT_3D_SIZE 99\n" + "0 0 0\n" * (99 ** 3))

    def test_clamps_out_of_domain_values(self):
        lut = parse_cube(IDENTITY_2.replace("1 1 1", "1.4 1 -0.2"))
        self.assertEqual(lut["table"][7], (1.0, 1.0, 0.0))

    def test_intensity_blend_endpoints_and_midpoint(self):
        warm = parse_cube(WARM_2)
        self.assertEqual(blend_with_identity(warm, 1.0)["table"], warm["table"])
        identity = blend_with_identity(warm, 0.0)
        self.assertEqual(identity["table"], parse_cube(IDENTITY_2)["table"])
        half = blend_with_identity(warm, 0.5)
        # Black corner: warm lifts red to 0.1, identity holds 0 — half is 0.05.
        self.assertAlmostEqual(half["table"][0][0], 0.05, places=6)
        # Blue corner keeps half the blue cut: (1 + 0.8) / 2.
        self.assertAlmostEqual(half["table"][4][2], 0.9, places=6)

    def test_write_round_trips(self):
        warm = parse_cube(WARM_2)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "warm.cube"
            write_cube(warm, path)
            again = parse_cube(path.read_text())
        for a, b in zip(warm["table"], again["table"]):
            for x, y in zip(a, b):
                self.assertAlmostEqual(x, y, places=5)


class ChainTests(unittest.TestCase):
    def test_lut_lands_between_corrections_and_finishing(self):
        chain = build_adjust_filter_chain({
            "fade": 30,
            "clarity": 20,
            "lut": {"assetId": 5, "path": "/tmp/editube_luts/lut_abc.cube"},
        })
        joined = ",".join(chain)
        lut_at = joined.index("lut3d")
        self.assertGreater(lut_at, joined.index("curves"))
        self.assertLess(lut_at, joined.index("unsharp"))
        self.assertIn("interp=tetrahedral", joined)

    def test_no_path_no_filter(self):
        # A raw client reference (assetId only) must never reach ffmpeg —
        # only the resolver's server-derived path counts.
        chain = build_adjust_filter_chain({"lut": {"assetId": 5, "intensity": 100}})
        self.assertEqual([f for f in chain if "lut3d" in f], [])

    def test_keyframed_slices_carry_the_lut(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "warm.cube"
            write_cube(parse_cube(WARM_2), path)
            chain = build_keyframed_adjust_filter_chain({
                "lut": {"assetId": 5, "path": str(path)},
                "keyframes": {"adjust.contrast": [{"t": 0, "v": -20}, {"t": 4, "v": 30}]},
            }, 4)
        lut_filters = [f for f in chain if "lut3d" in f]
        self.assertTrue(lut_filters)
        self.assertTrue(all("enable=" in f for f in lut_filters))

    def test_ffmpeg_accepts_the_lut_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "warm.cube"
            write_cube(parse_cube(WARM_2), path)
            chain = build_adjust_filter_chain({
                "contrast": 10,
                "vignette": 20,
                "lut": {"assetId": 5, "path": str(path)},
            })
            probe = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=gray:s=64x64:d=0.1",
                    "-vf", ",".join(chain),
                    "-frames:v", "1", "-f", "null", "-",
                ],
                capture_output=True,
                timeout=30,
            )
        self.assertEqual(probe.returncode, 0, probe.stderr.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main()
