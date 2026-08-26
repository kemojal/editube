"""The capability manifest must stay true to what the editor and render support.

The manifest is what the director is told it may ask for. If it drifts from the
code — a preset renamed in TypeScript, an easing removed from the export — the
director keeps emitting values that no longer render, and the failure surfaces
as B-roll that silently does not animate rather than as an error.

So these tests read the actual sources of truth: the TypeScript constants the
editor uses, and the allow-lists inside `rough_cut_export.py`. Parsing lives
here rather than in the production path on purpose — a fragile regex that breaks
a test is a nuisance, the same regex breaking a director run is an outage.
"""

from __future__ import annotations

import json
import pathlib
import re
import unittest

from app.jobs import rough_cut_export as rce
from app.services import director_manifest as manifest

FRONTEND = (
    pathlib.Path(__file__).resolve().parents[2]
    / "editube-frontend"
    / "app"
    / "(sites)"
    / "dashboard"
    / "rough-cut"
)


def _read(relative: str) -> str:
    return (FRONTEND / relative).read_text()


class SourceOfTruthTests(unittest.TestCase):
    """Every value offered must exist in the editor and survive the render."""

    def test_the_frontend_is_where_this_test_thinks_it_is(self) -> None:
        """Guards the rest: a moved directory would silently pass everything."""
        self.assertTrue((FRONTEND / "_lib" / "animation" / "clip-animation.ts").exists())

    def test_every_offered_animation_exists_in_the_editor(self) -> None:
        source = _read("_lib/animation/clip-animation.ts")
        declared = set(re.findall(r'\{\s*id:\s*"([a-z-]+)"', source))
        self.assertTrue(declared, "could not parse CLIP_ANIMATION_PRESETS")
        missing = set(manifest.ANIMATION_PRESETS) - declared
        self.assertEqual(missing, set(), f"offered but not in the editor: {missing}")

    def test_every_offered_animation_is_supported_by_the_render(self) -> None:
        source = pathlib.Path(rce.__file__).read_text()
        allowed = set(
            re.search(r"allowed = \{([^}]*)\}", source).group(1).replace('"', "").replace(" ", "").split(",")
        )
        missing = set(manifest.ANIMATION_PRESETS) - allowed
        self.assertEqual(missing, set(), f"offered but the export drops it: {missing}")

    def test_offered_animations_can_actually_be_used_as_enter_or_exit(self) -> None:
        """A combo-only preset placed as an entrance simply does nothing."""
        source = _read("_lib/animation/clip-animation.ts")
        combo_only = set()
        for entry in re.findall(r'\{\s*id:\s*"([a-z-]+)",[^}]*modes:\s*\[([^\]]*)\]', source):
            preset, modes = entry
            names = {m.strip().strip('"') for m in modes.split(",")}
            if names == {"combo"}:
                combo_only.add(preset)
        self.assertTrue(combo_only, "could not parse preset modes")
        self.assertEqual(set(manifest.ANIMATION_PRESETS) & combo_only, set())

    def test_every_offered_easing_exists_in_both_implementations(self) -> None:
        fixture = json.loads(
            (pathlib.Path(__file__).parent / "fixtures" / "easing_curves.json").read_text()
        )
        missing = set(manifest.EASING_CURVES) - set(fixture["curves"])
        self.assertEqual(missing, set(), f"offered but unimplemented: {missing}")
        for name in manifest.EASING_CURVES:
            with self.subTest(easing=name):
                # Anything the export does not recognise silently becomes linear.
                self.assertNotEqual(
                    rce._eased_ratio("P", name),
                    "P" if name != "linear" else object(),
                    f"{name} falls through to linear in the export",
                )

    def test_every_offered_aspect_ratio_exists_in_the_editor(self) -> None:
        source = _read("_lib/ai-media-models.ts")
        declared = set(
            re.search(r"ASPECT_RATIOS = \[([^\]]*)\]", source).group(1).replace('"', "").replace(" ", "").split(",")
        )
        missing = set(manifest.ASPECT_RATIOS) - declared
        self.assertEqual(missing, set(), f"offered but unknown to the editor: {missing}")


class DeliberateExclusionTests(unittest.TestCase):
    """Things the editor supports that the director must not be offered yet.

    Each of these is a decision recorded in docs/ai_creative_director.md. The
    tests exist so the decision has to be revisited deliberately rather than
    undone by someone tidying up the lists.
    """

    def test_focus_is_withheld_until_the_export_renders_blur(self) -> None:
        """C2: the export drops the blur channel, so `focus` loses its point."""
        self.assertNotIn("focus", manifest.ANIMATION_PRESETS)

    def test_auto_aspect_is_withheld_so_shots_match_the_target_format(self) -> None:
        self.assertNotIn("auto", manifest.ASPECT_RATIOS)

    def test_stock_footage_is_withheld_while_unimplemented(self) -> None:
        self.assertNotIn("stock", manifest.ASSET_SOURCES)

    def test_reusing_project_media_is_withheld_until_the_model_can_see_it(self) -> None:
        """Offering a library the context never lists is asking it to guess.

        Every such directive would name footage that may not exist and resolve
        to nothing. It returns when the context carries a media inventory.
        """
        self.assertNotIn("project-media", manifest.ASSET_SOURCES)

    def test_only_broll_directives_are_offered_in_v1(self) -> None:
        """Transitions and titles need M6; emphasis needs range splitting."""
        self.assertEqual(set(manifest.DIRECTIVE_TYPES), {"broll"})

    def test_the_a_roll_track_is_never_a_b_roll_target(self) -> None:
        self.assertNotIn("V1", manifest.TRACKS)


class RenderedManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = manifest.render_manifest(aspect="9:16", max_images=12, max_videos=2)

    def test_the_projects_own_aspect_is_stated_not_just_listed(self) -> None:
        """A list of options is not an instruction; the target has to be named."""
        self.assertIn("**9:16**", self.text)

    def test_the_budget_is_in_the_prompt_not_only_enforced_afterwards(self) -> None:
        self.assertIn("**12**", self.text)
        self.assertIn("**2**", self.text)

    def test_every_offered_value_appears_in_the_text(self) -> None:
        for group in (
            manifest.ANIMATION_PRESETS,
            manifest.EASING_CURVES,
            manifest.TRACKS,
            manifest.ASSET_SOURCES,
        ):
            for value in group:
                with self.subTest(value=value):
                    self.assertIn(f"`{value}`", self.text)

    def test_withheld_values_never_leak_into_the_text(self) -> None:
        for withheld in ("`focus`", "`stock`", "`V1`", "`auto`"):
            with self.subTest(value=withheld):
                self.assertNotIn(withheld, self.text)

    def test_the_manifest_is_long_enough_to_be_worth_caching(self) -> None:
        """Opus 5 will not cache a prefix under ~512 tokens.

        The manifest sits in front of the cache breakpoint precisely so the
        later passes read it back cheaply; if it were short enough to fall under
        the minimum, every pass would re-bill it in full.
        """
        self.assertGreater(len(self.text) / 4, 512)


class MachineReadableTests(unittest.TestCase):
    def test_it_agrees_with_the_prose(self) -> None:
        data = manifest.as_dict()
        self.assertEqual(data["animationPresets"], list(manifest.ANIMATION_PRESETS))
        self.assertEqual(data["easingCurves"], list(manifest.EASING_CURVES))
        self.assertEqual(data["planVersion"], manifest.PLAN_VERSION)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
