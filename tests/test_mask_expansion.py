"""Cross-language parity for Task 2 (Expansion), driven by the shared golden
fixture `editube-frontend/docs/fixtures/mask-expansion-golden.json`.

`expansion_radius()` in `app/services/mask_geometry.py` must produce the same
VIEWBOX-space radius as `expansionRadius()` in mask-geometry.ts for the same
mask (see the contract comment on `EXPANSION_RADIUS_FACTOR` in both files).
This also asserts the radius->Pillow-kernel rounding contract used by
`app/services/mask_matte.py`'s `render_matte_frame`: `MaxFilter`/`MinFilter`
need an odd integer kernel, not a continuous radius, so the pixel radius is
rounded to the nearest integer before `kernel = 2*r + 1` is built. One
fixture case (`"fractional radius rounds to the nearest Pillow kernel"`)
exists specifically to prove that rounding, not just the trivial exact case.

Vendoring follows the same pattern as `test_mask_geometry.py`: a copy lives
in `tests/fixtures/` so this suite doesn't depend on the monorepo layout,
with a drift check against the frontend's copy when it's reachable.
"""

import hashlib
import json
import unittest
from pathlib import Path

from app.services.mask_geometry import expansion_radius

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mask-expansion-golden.json"
SOURCE_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "editube-frontend"
    / "docs"
    / "fixtures"
    / "mask-expansion-golden.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class VendoredExpansionFixtureDriftTests(unittest.TestCase):
    def test_vendored_copy_matches_the_frontend_source_when_reachable(self):
        if not SOURCE_FIXTURE.exists():
            raise unittest.SkipTest(
                f"monorepo layout not present ({SOURCE_FIXTURE} not found) -- "
                "drift check only applies when the frontend's fixture is reachable"
            )
        if not FIXTURE.exists():
            self.fail(f"vendored fixture missing at {FIXTURE}")
        self.assertEqual(
            _sha256(FIXTURE),
            _sha256(SOURCE_FIXTURE),
            "vendored fixture has drifted from the frontend's copy -- "
            f"re-copy {SOURCE_FIXTURE} to {FIXTURE}",
        )


class ExpansionRadiusParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not FIXTURE.exists():
            raise RuntimeError(
                f"vendored golden fixture missing at {FIXTURE}; this repo carries "
                "its own copy so parity testing doesn't depend on the monorepo layout."
            )
        cls.cases = json.loads(FIXTURE.read_text())["cases"]

    def test_has_a_nonzero_expansion_case(self):
        self.assertTrue(any(case["expansion"] != 0 for case in self.cases))

    def test_matches_golden_radius_and_kernel(self):
        for case in self.cases:
            with self.subTest(case["name"]):
                mask = {"width": case["width"], "height": case["height"], "expansion": case["expansion"]}
                radius = expansion_radius(mask)
                self.assertAlmostEqual(radius, case["expectRadiusViewbox"], places=3)

                expansion = case["expansion"]
                operator = None if expansion == 0 else ("dilate" if expansion > 0 else "erode")
                self.assertEqual(operator, case["expectOperator"])

                radius_px = radius * ((case["sx"] + case["sy"]) / 2)
                self.assertAlmostEqual(radius_px, case["expectRadiusPx"], places=3)

                r = round(radius_px)
                kernel = (2 * r + 1) if r > 0 else None
                self.assertEqual(kernel, case["expectKernel"])


if __name__ == "__main__":
    unittest.main()
