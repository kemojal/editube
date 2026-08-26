"""The rules the model is not trusted to keep.

Forced tool use already guarantees the plan is shape-valid, so nothing here
re-tests JSON parsing. What is tested is the relational stuff a schema cannot
express — overlap, pacing, budget, anchors that cite real words — and the choice
to *drop* rather than repair. A B-roll shot in roughly the right place is worse
than no shot: it cuts mid-syllable and the user has to find and undo it.
"""

from __future__ import annotations

import unittest

from app.services import director_manifest as manifest
from app.services.director_plan import (
    PlanRejected,
    brief_schema,
    directives_schema,
    plan_schema,
    validate_plan,
)

def _directive(index: int, start: float, end: float, **overrides):
    directive = {
        "id": f"d{index}",
        "type": "broll",
        "start": start,
        "end": end,
        "anchor": {"quote": f"quote {index}", "segmentId": f"s{index}"},
        "track": "V2",
        "asset": {
            "source": "generate-image",
            "prompt": "an overhead shot of a cluttered desk at dusk",
            "aspectRatio": "16:9",
        },
        "animationIn": {"preset": "fade", "durationSeconds": 0.35},
        "animationOut": {"preset": "fade", "durationSeconds": 0.35},
        "confidence": 0.8,
        "why": "The speaker names a place; show it.",
    }
    directive.update(overrides)
    return directive


def _plan(*directives, **overrides):
    plan = {
        "version": manifest.PLAN_VERSION,
        "brief": {
            "genre": "explainer",
            "audience": "developers",
            "tone": ["warm"],
            "pacing": "medium",
            "visualMotifs": ["warm practical light"],
            "houseStylePrefix": "Cinematic still, 35mm, shallow depth of field.",
            "rationale": "because",
        },
        "beats": [],
        "directives": list(directives),
    }
    plan.update(overrides)
    return plan


def _validate(plan, *, runtime=300.0, images=20, videos=3, context=None):
    return validate_plan(
        plan,
        runtime_seconds=runtime,
        context=context,
        max_images=images,
        max_videos=videos,
    )


class SchemaTests(unittest.TestCase):
    def test_enums_come_from_the_manifest_so_they_cannot_drift(self) -> None:
        directive = plan_schema()["properties"]["directives"]["items"]
        self.assertEqual(
            directive["properties"]["track"]["enum"], list(manifest.TRACKS)
        )
        self.assertEqual(
            directive["properties"]["animationIn"]["properties"]["preset"]["enum"],
            list(manifest.ANIMATION_PRESETS),
        )

    def test_withheld_values_are_not_in_the_schema_either(self) -> None:
        """The manifest and the schema must withhold the same things."""
        directive = plan_schema()["properties"]["directives"]["items"]
        self.assertNotIn("focus", directive["properties"]["animationIn"]["properties"]["preset"]["enum"])
        self.assertNotIn("stock", directive["properties"]["asset"]["properties"]["source"]["enum"])

    def test_nested_objects_forbid_extra_properties(self) -> None:
        """`strict` tool use rejects a schema that permits unknown keys."""
        directive = plan_schema()["properties"]["directives"]["items"]
        self.assertIs(directive["additionalProperties"], False)
        self.assertIs(directive["properties"]["asset"]["additionalProperties"], False)
        self.assertIs(directive["properties"]["anchor"]["additionalProperties"], False)

    def test_an_anchor_is_mandatory(self) -> None:
        """Timing alone does not survive a re-cut; the anchor is what does."""
        directive = plan_schema()["properties"]["directives"]["items"]
        self.assertIn("anchor", directive["required"])

    def test_a_reason_is_mandatory_because_the_user_reads_it(self) -> None:
        directive = plan_schema()["properties"]["directives"]["items"]
        self.assertIn("why", directive["required"])

    def test_the_two_pass_schemas_are_slices_of_the_whole(self) -> None:
        whole = plan_schema()["properties"]
        self.assertEqual(brief_schema()["properties"]["brief"], whole["brief"])
        self.assertEqual(directives_schema()["properties"]["directives"], whole["directives"])


class WholePlanRejectionTests(unittest.TestCase):
    def test_an_unknown_version_is_refused_outright(self) -> None:
        """Different directive semantics must not reach this compiler."""
        with self.assertRaises(PlanRejected):
            _validate(_plan(version=99))

    def test_a_plan_with_no_house_style_is_refused(self) -> None:
        """Twelve unprefixed prompts are twelve stock photos, not one film."""
        plan = _plan()
        plan["brief"]["houseStylePrefix"] = "   "
        with self.assertRaises(PlanRejected):
            _validate(plan)


class PacingTests(unittest.TestCase):
    def test_shots_that_overlap_are_dropped(self) -> None:
        result = _validate(_plan(_directive(1, 10, 14), _directive(2, 12, 16)))
        self.assertEqual([d["id"] for d in result.directives], ["d1"])
        self.assertIn("overlaps", result.warnings[0])

    def test_shots_that_crowd_each_other_are_dropped(self) -> None:
        gap = manifest.MIN_GAP_SECONDS
        result = _validate(_plan(_directive(1, 10, 14), _directive(2, 14 + gap / 2, 18 + gap / 2)))
        self.assertEqual([d["id"] for d in result.directives], ["d1"])

    def test_a_dropped_shot_frees_the_gap_it_was_holding(self) -> None:
        """Gaps are measured against survivors, not against the model's list.

        Otherwise one rejected shot would push out its innocent neighbour too.
        """
        gap = manifest.MIN_GAP_SECONDS
        result = _validate(
            _plan(
                _directive(1, 10, 14),
                _directive(2, 15, 19),                       # too close to d1, dropped
                _directive(3, 14 + gap + 1, 18 + gap + 1),   # fine relative to d1
            )
        )
        self.assertEqual([d["id"] for d in result.directives], ["d1", "d3"])

    def test_an_overlong_shot_is_trimmed_rather_than_dropped(self) -> None:
        """The moment it was chosen for is still right."""
        result = _validate(_plan(_directive(1, 10, 10 + manifest.MAX_BROLL_SECONDS + 5)))
        self.assertEqual(len(result.directives), 1)
        self.assertAlmostEqual(
            result.directives[0]["end"] - result.directives[0]["start"],
            manifest.MAX_BROLL_SECONDS,
        )
        self.assertIn("Shortened", result.warnings[0])

    def test_a_shot_too_short_to_register_is_dropped(self) -> None:
        result = _validate(_plan(_directive(1, 10, 10.4)))
        self.assertEqual(result.directives, [])

    def test_a_shot_past_the_end_of_the_cut_is_dropped(self) -> None:
        result = _validate(_plan(_directive(1, 295, 299)), runtime=100.0)
        self.assertEqual(result.directives, [])

    def test_coverage_is_capped(self) -> None:
        """Past a point it stops being B-roll and becomes the programme."""
        spaced = [
            _directive(i, i * 20.0, i * 20.0 + manifest.MAX_BROLL_SECONDS)
            for i in range(1, 11)
        ]
        result = _validate(_plan(*spaced), runtime=200.0)
        covered = sum(d["end"] - d["start"] for d in result.directives)
        self.assertLessEqual(covered, 200.0 * manifest.MAX_COVERAGE_RATIO)
        self.assertTrue(any("coverage" in w for w in result.warnings))


class AnchorTests(unittest.TestCase):
    """Anchors are checked against the real transcript, not just for shape."""

    def setUp(self) -> None:
        from app.services.director_context import build_context

        self.context = build_context(
            segments=[
                {"start": 0.0, "end": 4.0, "text": "Most teams lose two days a week"},
                {"start": 4.0, "end": 8.0, "text": "and nobody can say where it went"},
            ],
            keep_ranges=[],
            source_duration=8.0,
            aspect="16:9",
        )

    def test_a_shot_with_no_anchor_quote_is_dropped(self) -> None:
        result = _validate(
            _plan(_directive(1, 10, 14, anchor={"quote": "", "segmentId": "s0"}))
        )
        self.assertEqual(result.directives, [])

    def test_an_invented_quote_is_dropped(self) -> None:
        """A misquote looks precise and resolves to nowhere.

        That is strictly worse than no anchor, which is why it is a drop rather
        than a silent fallback to the model's own timing.
        """
        result = _validate(
            _plan(_directive(1, 2, 4, anchor={"quote": "never said this", "segmentId": "s0"})),
            runtime=60.0,
            context=self.context,
        )
        self.assertEqual(result.directives, [])
        self.assertIn("not in the transcript", result.warnings[0])

    def test_a_real_quote_survives(self) -> None:
        result = _validate(
            _plan(_directive(1, 2, 4, anchor={"quote": "two days a week", "segmentId": "s0"})),
            runtime=60.0,
            context=self.context,
        )
        self.assertEqual(len(result.directives), 1)

    def test_a_wrong_segment_id_does_not_lose_a_good_quote(self) -> None:
        """A misattributed line is recoverable; the quote is what matters."""
        result = _validate(
            _plan(_directive(1, 2, 4, anchor={"quote": "where it went", "segmentId": "s0"})),
            runtime=60.0,
            context=self.context,
        )
        self.assertEqual(len(result.directives), 1)

    def test_anchors_are_only_checked_when_a_transcript_is_supplied(self) -> None:
        """Pass B validates before the context is threaded through everywhere."""
        result = _validate(
            _plan(_directive(1, 10, 14, anchor={"quote": "anything", "segmentId": "s0"}))
        )
        self.assertEqual(len(result.directives), 1)


class BudgetTests(unittest.TestCase):
    def test_still_generation_is_capped(self) -> None:
        spaced = [_directive(i, i * 20.0, i * 20.0 + 4) for i in range(1, 8)]
        result = _validate(_plan(*spaced), runtime=1000.0, images=3)
        self.assertEqual(len(result.directives), 3)
        self.assertTrue(any("still budget" in w for w in result.warnings))

    def test_moving_shots_are_capped_separately(self) -> None:
        """Veo minutes are the dominant cost; stills must not consume the quota."""
        directives = []
        for i in range(1, 6):
            source = "generate-video" if i <= 4 else "generate-image"
            directives.append(
                _directive(i, i * 20.0, i * 20.0 + 4, asset={
                    "source": source, "prompt": "p", "aspectRatio": "16:9",
                })
            )
        result = _validate(_plan(*directives), runtime=1000.0, videos=2)
        kinds = [d["asset"]["source"] for d in result.directives]
        self.assertEqual(kinds.count("generate-video"), 2)
        self.assertEqual(kinds.count("generate-image"), 1)

    def test_reusing_project_media_costs_nothing(self) -> None:
        directives = [
            _directive(i, i * 20.0, i * 20.0 + 4, asset={
                "source": "project-media", "prompt": "p", "aspectRatio": "16:9",
            })
            for i in range(1, 6)
        ]
        result = _validate(_plan(*directives), runtime=1000.0, images=0, videos=0)
        self.assertEqual(len(result.directives), 5)


class ReportingTests(unittest.TestCase):
    def test_warnings_name_the_moment_not_the_id(self) -> None:
        """The user recognises the quote; `d7` means nothing to them."""
        result = _validate(
            _plan(
                _directive(1, 10, 14),
                _directive(2, 12, 16, anchor={"quote": "two days a week", "segmentId": "s0"}),
            )
        )
        self.assertIn('"two days a week"', result.warnings[0])

    def test_an_empty_plan_says_the_cut_is_untouched(self) -> None:
        result = _validate(_plan())
        self.assertTrue(any("left as-is" in w for w in result.warnings))

    def test_duplicate_ids_are_dropped(self) -> None:
        result = _validate(_plan(_directive(1, 10, 14), _directive(1, 40, 44)))
        self.assertEqual(len(result.directives), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
