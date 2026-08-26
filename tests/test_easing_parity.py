"""The render's easing curves must match the editor's, exactly.

`_eased_ratio` builds the ffmpeg expression the MP4 is rendered with;
`easingRatio` in `_lib/keyframes/clip-keyframes.ts` is what the viewer previews
with. Two hand-written implementations of the same formulas drift, and the drift
is invisible until someone exports and finds the motion is not what they
approved. `tests/fixtures/easing_curves.json` is the contract both sides assert
against.

The interesting test here is `FfmpegEvaluatorTests`: it does not re-read the
expression in Python, it hands it to ffmpeg and reads the number back out. A
formula can be right in Python and still be wrong as an ffmpeg expression —
operator precedence, a function that does not exist, a negative base under
`pow` — and only the real evaluator can say.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import unittest

from app.jobs import rough_cut_export as rce

FIXTURE = json.loads(
    (pathlib.Path(__file__).parent / "fixtures" / "easing_curves.json").read_text()
)

#: Every easing the editor's `ClipKeyframeEasing` union allows.
DECLARED = {
    "linear", "ease-in", "ease-out", "ease-in-out", "hold",
    "smooth", "glide", "snappy", "anticipate", "settle", "overshoot",
}


def _python_eval(expr: str, p: float) -> float:
    """Evaluate a generated expression the way ffmpeg would, in Python.

    Only the small grammar `_eased_ratio` actually emits is supported — this is
    a cross-check on the formula, not an ffmpeg implementation.
    """
    scope = {
        "pow": pow,
        "lt": lambda a, b: 1.0 if a < b else 0.0,
        "if_": lambda c, a, b: a if c else b,
        "P": p,
    }
    # `if(a,b,c)` is not Python; rewrite to a call we can evaluate.
    return float(eval(expr.replace("if(", "if_("), {"__builtins__": {}}, scope))  # noqa: S307


class EasingFixtureTests(unittest.TestCase):
    def test_the_fixture_covers_every_declared_easing(self) -> None:
        """A curve added to the type but not the fixture would ship untested."""
        self.assertEqual(set(FIXTURE["curves"]), DECLARED)

    def test_every_curve_matches_the_shared_fixture(self) -> None:
        for name, expected in FIXTURE["curves"].items():
            expr = rce._eased_ratio("P", name)
            for p, want in zip(FIXTURE["samples"], expected):
                with self.subTest(curve=name, p=p):
                    self.assertAlmostEqual(_python_eval(expr, p), want, places=10)

    def test_the_original_curves_stay_quadratic(self) -> None:
        """Saved projects must render exactly as they did before the new curves."""
        for p in FIXTURE["samples"]:
            self.assertAlmostEqual(_python_eval(rce._eased_ratio("P", "ease-in"), p), p * p, places=12)
            self.assertAlmostEqual(
                _python_eval(rce._eased_ratio("P", "ease-out"), p), 1 - (1 - p) ** 2, places=12
            )

    def test_an_unknown_easing_falls_back_to_linear(self) -> None:
        self.assertEqual(rce._eased_ratio("P", "nonsense"), "P")

    def test_hold_never_moves(self) -> None:
        self.assertEqual(rce._eased_ratio("P", "hold"), "0")

    def test_the_named_curves_behave_as_named(self) -> None:
        curves = FIXTURE["curves"]
        # Anticipation dips below its start before travelling.
        self.assertLess(min(curves["anticipate"]), -0.05)
        # Back-eases overshoot, and `overshoot` overshoots more than `settle`.
        self.assertGreater(max(curves["settle"]), 1.05)
        self.assertGreater(max(curves["overshoot"]), max(curves["settle"]))
        # Every curve but `hold` spans 0 to 1.
        for name, values in curves.items():
            if name == "hold":
                continue
            with self.subTest(curve=name):
                self.assertAlmostEqual(values[0], 0.0, places=10)
                self.assertAlmostEqual(values[-1], 1.0, places=10)


@unittest.skipIf(shutil.which("ffmpeg") is None, "ffmpeg is not installed")
class FfmpegEvaluatorTests(unittest.TestCase):
    """Hand each expression to ffmpeg and read the number back.

    Painting the value into a 16-bit luma plane and reading the raw byte pair is
    the cheapest way to get ffmpeg's expression evaluator to tell us what it
    computed. The output range is mapped through [-0.2, 1.2] so the two curves
    that leave [0,1] -- `anticipate` below and `settle`/`overshoot` above -- stay
    representable instead of being clipped to the very thing under test.
    """

    #: One 16-bit step across the mapped range. Anything larger is a real error.
    QUANTIZATION = 1.4 / 65535.0

    def _ffmpeg_eval(self, expr: str, p: float) -> float:
        mapped = f"(((({expr})+0.2)/1.4))".replace("P", repr(p))
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=black:s=2x2:d=0.05:r=20",
                # geq must run at 16-bit depth or its output clamps at 255.
                "-vf", f"format=gray16le,geq=lum='clip({mapped}*65535,0,65535)'",
                "-pix_fmt", "gray16le", "-frames:v", "1", "-f", "rawvideo", "-",
            ],
            capture_output=True,
            check=False,
        )
        raw = result.stdout[:2]
        if len(raw) < 2:
            self.fail(f"ffmpeg rejected the expression: {result.stderr.decode()[:400]}")
        return (int.from_bytes(raw, "little") / 65535.0) * 1.4 - 0.2

    def test_ffmpeg_agrees_with_the_fixture_for_every_curve(self) -> None:
        for name, expected in FIXTURE["curves"].items():
            expr = rce._eased_ratio("P", name)
            # The endpoints and the middle are where a precedence or sign error
            # shows; sampling all 21 points per curve would run 231 ffmpeg
            # processes for no extra signal.
            for index in (0, 5, 10, 15, 20):
                p, want = FIXTURE["samples"][index], expected[index]
                with self.subTest(curve=name, p=p):
                    self.assertAlmostEqual(
                        self._ffmpeg_eval(expr, p), want, delta=2 * self.QUANTIZATION
                    )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
