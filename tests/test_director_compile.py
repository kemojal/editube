"""Placing the planned shots, and taking them back off again.

The compiler is where director time becomes source time, and that conversion is
the highest-risk arithmetic in the feature: a shot placed with the wrong map
lands on the wrong sentence, and nothing about the result looks wrong — a B-roll
clip in the wrong place looks exactly like a B-roll clip.

So the tests here are mostly about coordinates, about what happens when a cut
falls inside a shot, and about the three properties the merge has to hold:
re-applying changes nothing, a failed asset places nothing, and reverting
removes exactly what was added and no more.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.services.director_compile import compile_plan, ensure_broll_track, revert_plan
from app.services.director_context import build_context

SEGMENTS = [
    {"start": 0.0, "end": 6.0, "text": "Most teams lose two days a week to meetings"},
    {"start": 6.0, "end": 12.0, "text": "and nobody can say where the time actually went"},
    {"start": 12.0, "end": 20.0, "text": "so we built something to find out where it goes"},
]


def _asset(asset_id=1, kind="image", status="ready", url="https://cdn.test/a.png", duration=None):
    return SimpleNamespace(
        id=asset_id, kind=kind, status=status, url=url, duration_seconds=duration
    )


def _shot(index=1, start=1.0, end=4.0, quote="two days a week", segment="s0", **overrides):
    shot = {
        "id": f"d{index}",
        "type": "broll",
        "start": start,
        "end": end,
        "anchor": {"quote": quote, "segmentId": segment},
        "track": "V2",
        "asset": {"source": "generate-image", "prompt": "a cluttered desk at dusk", "aspectRatio": "16:9"},
        "animationIn": {"preset": "fade", "durationSeconds": 0.35},
        "animationOut": {"preset": "fade", "durationSeconds": 0.4},
        "framing": {"kenBurns": {"from": 1.0, "to": 1.08, "easing": "glide"}},
        "confidence": 0.8,
        "why": "because",
    }
    shot.update(overrides)
    return shot


def _compile(draft=None, shots=None, assets=None, keep_ranges=None, plan_id=7):
    context = build_context(
        segments=SEGMENTS,
        keep_ranges=keep_ranges if keep_ranges is not None else [],
        source_duration=20.0,
        aspect="16:9",
    )
    shots = shots if shots is not None else [_shot()]
    assets = assets if assets is not None else {s["id"]: _asset(i + 1) for i, s in enumerate(shots)}
    return compile_plan(
        draft or {},
        {"version": 1, "brief": {}, "beats": [], "directives": shots},
        context=context,
        assets_by_directive=assets,
        plan_id=plan_id,
        applied_at="2026-08-11T00:00:00Z",
    ), context


class TrackTests(unittest.TestCase):
    def _default_tracks(self):
        return [
            {"id": "track-text-1", "kind": "text", "label": "TX1", "order": 0},
            {"id": "track-video-1", "kind": "video", "label": "V1", "order": 1},
            {"id": "track-audio-1", "kind": "audio", "label": "A1", "order": 2},
        ]

    def test_broll_sits_above_the_a_roll_but_under_the_titles(self) -> None:
        """Above V1 is what B-roll means; a title it covers is a title unread."""
        tracks, track_id = ensure_broll_track(self._default_tracks(), "V2")
        by_label = {t["label"]: t["order"] for t in tracks}
        self.assertLess(by_label["TX1"], by_label["V2"])
        self.assertLess(by_label["V2"], by_label["V1"])
        self.assertEqual(track_id, "track-video-2")

    def test_audio_stays_below_the_visual_stack(self) -> None:
        tracks, _ = ensure_broll_track(self._default_tracks(), "V2")
        self.assertEqual(tracks[-1]["kind"], "audio")

    def test_orders_come_back_as_sequential_indices(self) -> None:
        """The editor renumbers on load; storing gaps would just churn."""
        tracks, _ = ensure_broll_track(self._default_tracks(), "V2")
        self.assertEqual([t["order"] for t in tracks], [0, 1, 2, 3])

    def test_an_existing_broll_track_is_reused(self) -> None:
        existing = self._default_tracks() + [
            {"id": "track-video-9", "kind": "video", "label": "V2", "order": 0}
        ]
        _, track_id = ensure_broll_track(existing, "V2")
        self.assertEqual(track_id, "track-video-9")


class PlacementTests(unittest.TestCase):
    def test_a_shot_lands_on_the_words_it_was_anchored_to(self) -> None:
        result, context = _compile()
        item = result.draft["timelineMediaItems"][0]
        words = context.words_by_segment[0]
        self.assertAlmostEqual(item["start"], words[3].start, places=3)

    def test_the_clip_names_itself_after_the_shot_not_the_directive(self) -> None:
        """The user is scanning a timeline; `d7` tells them nothing."""
        result, _ = _compile()
        self.assertIn("cluttered desk", result.draft["timelineMediaItems"][0]["name"])

    def test_the_source_discriminator_is_set(self) -> None:
        """Without it the render composites the A-roll in the shot's place (G2)."""
        result, _ = _compile(assets={"d1": _asset(42)})
        item = result.draft["timelineMediaItems"][0]
        self.assertEqual(item["sourceKind"], "generated")
        self.assertEqual(item["sourceId"], 42)

    def test_broll_ships_muted(self) -> None:
        """The A-roll is still the programme."""
        result, _ = _compile()
        self.assertIs(result.draft["timelineMediaItems"][0]["audioEnabled"], False)

    def test_the_shot_goes_on_the_broll_track(self) -> None:
        result, _ = _compile()
        item = result.draft["timelineMediaItems"][0]
        tracks = {t["id"]: t for t in result.draft["timelineTracks"]}
        self.assertEqual(tracks[item["trackId"]]["label"], "V2")


class CoordinateTests(unittest.TestCase):
    """Director time in, source time out. The riskiest arithmetic here."""

    def test_placement_accounts_for_earlier_cuts(self) -> None:
        """A shot after a cut must not drift by the length of what was removed."""
        keep = [{"start": 0, "end": 6}, {"start": 12, "end": 20}]
        result, context = _compile(
            shots=[_shot(quote="to find out", segment="s2", start=7.0, end=10.0)],
            keep_ranges=keep,
        )
        item = result.draft["timelineMediaItems"][0]
        # "to find out" lives at ~16s of source, which is ~10s into the cut.
        self.assertGreater(item["start"], 12.0)

    def test_a_shot_spanning_a_cut_covers_the_gap_in_source_time(self) -> None:
        """The export intersects with keepRanges and splits it, so the shot
        ripples with the cut instead of drifting off it (§6 rule 3)."""
        keep = [{"start": 0, "end": 6}, {"start": 12, "end": 20}]
        result, _ = _compile(
            shots=[_shot(quote="days a week", segment="s0", start=3.0, end=8.0)],
            keep_ranges=keep,
        )
        item = result.draft["timelineMediaItems"][0]
        # Five seconds of screen time starting inside the first kept range has
        # to reach past the removed 6–12s gap.
        self.assertGreater(item["end"], 12.0)

    def test_play_duration_records_the_intended_screen_time(self) -> None:
        """The source span can be longer when a cut falls inside it, so this is
        the only record of what the director actually asked for."""
        keep = [{"start": 0, "end": 6}, {"start": 12, "end": 20}]
        result, _ = _compile(
            shots=[_shot(quote="days a week", segment="s0", start=3.0, end=8.0)],
            keep_ranges=keep,
        )
        item = result.draft["timelineMediaItems"][0]
        self.assertAlmostEqual(item["playDuration"], 5.0, places=2)
        self.assertGreater(item["end"] - item["start"], item["playDuration"])

    def test_a_shot_anchored_to_removed_footage_says_it_was_cut(self) -> None:
        """Distinct from a misquote, because the user can act on this one.

        Footage they cut can be brought back; words nobody said cannot. Saying
        "could not find" for both sends them hunting a transcript for a line
        that is sitting right there.
        """
        result, _ = _compile(
            shots=[_shot(quote="nobody can say", segment="s1")],
            keep_ranges=[{"start": 0, "end": 6}],
        )
        self.assertEqual(result.draft["timelineMediaItems"], [])
        self.assertTrue(any("has since been cut" in w for w in result.warnings))

    def test_a_misquote_is_reported_differently_from_a_cut(self) -> None:
        result, _ = _compile(
            shots=[_shot(quote="never said this at all")],
            keep_ranges=[{"start": 0, "end": 6}],
        )
        self.assertTrue(any("Could not find" in w for w in result.warnings))
        self.assertFalse(any("has since been cut" in w for w in result.warnings))


class MotionTests(unittest.TestCase):
    def test_ken_burns_becomes_a_keyframed_scale_ramp(self) -> None:
        result, _ = _compile()
        attrs = result.draft["clipAttributes"]["media:dir7-d1"]
        track = attrs["keyframes"]["video.scale"]
        # `video.scale` is a percentage with 100 neutral, not a multiplier.
        self.assertAlmostEqual(track[0]["v"], 100.0)
        self.assertAlmostEqual(track[1]["v"], 108.0)
        self.assertEqual(track[0]["easing"], "glide")

    def test_the_ramp_spans_the_shots_screen_time(self) -> None:
        result, _ = _compile(shots=[_shot(start=2.0, end=6.0)])
        track = result.draft["clipAttributes"]["media:dir7-d1"]["keyframes"]["video.scale"]
        self.assertAlmostEqual(track[1]["t"], 4.0)

    def test_an_extreme_move_is_clamped_rather_than_dropped(self) -> None:
        """The intent was right even when the number was not."""
        result, _ = _compile(
            shots=[_shot(framing={"kenBurns": {"from": 1.0, "to": 9.0, "easing": "smooth"}})]
        )
        track = result.draft["clipAttributes"]["media:dir7-d1"]["keyframes"]["video.scale"]
        self.assertLessEqual(track[1]["v"], 140.0)

    def test_a_move_too_small_to_see_is_not_written(self) -> None:
        result, _ = _compile(
            shots=[_shot(framing={"kenBurns": {"from": 1.0, "to": 1.001, "easing": "smooth"}})]
        )
        self.assertNotIn("keyframes", result.draft["clipAttributes"]["media:dir7-d1"])

    def test_an_unknown_easing_falls_back_rather_than_rendering_linear(self) -> None:
        result, _ = _compile(
            shots=[_shot(framing={"kenBurns": {"from": 1.0, "to": 1.08, "easing": "bouncy"}})]
        )
        track = result.draft["clipAttributes"]["media:dir7-d1"]["keyframes"]["video.scale"]
        self.assertEqual(track[0]["easing"], "smooth")

    def test_enter_and_exit_animations_are_recorded(self) -> None:
        result, _ = _compile()
        animation = result.draft["clipAttributes"]["media:dir7-d1"]["animation"]
        self.assertEqual(animation["inPreset"], "fade")
        self.assertEqual(animation["outPreset"], "fade")


class SkipTests(unittest.TestCase):
    def test_a_failed_asset_places_nothing(self) -> None:
        """An empty clip is worse than an uninterrupted face."""
        result, _ = _compile(assets={"d1": _asset(status="failed", url="")})
        self.assertEqual(result.draft["timelineMediaItems"], [])
        self.assertEqual(result.placed, 0)
        self.assertTrue(result.warnings)

    def test_an_asset_still_generating_places_nothing(self) -> None:
        result, _ = _compile(assets={"d1": _asset(status="running")})
        self.assertEqual(result.draft["timelineMediaItems"], [])

    def test_a_shot_whose_quote_is_not_in_the_transcript_is_skipped(self) -> None:
        result, _ = _compile(shots=[_shot(quote="never said this")])
        self.assertEqual(result.draft["timelineMediaItems"], [])

    def test_one_bad_shot_does_not_lose_the_others(self) -> None:
        shots = [_shot(1), _shot(2, quote="where the time", segment="s1", start=8.0, end=11.0)]
        result, _ = _compile(shots=shots, assets={"d1": _asset(status="failed"), "d2": _asset(2)})
        self.assertEqual(result.placed, 1)
        self.assertEqual(result.draft["timelineMediaItems"][0]["id"], "dir7-d2")


class MergeTests(unittest.TestCase):
    def test_existing_clips_are_kept(self) -> None:
        draft = {
            "timelineMediaItems": [{"id": "mine", "track": "video", "trackId": "track-video-1"}],
            "clipAttributes": {"media:mine": {"mirror": True}},
        }
        result, _ = _compile(draft=draft)
        ids = [item["id"] for item in result.draft["timelineMediaItems"]]
        self.assertIn("mine", ids)
        self.assertIn("media:mine", result.draft["clipAttributes"])

    def test_applying_the_same_plan_twice_changes_nothing(self) -> None:
        """Ids are derived from the plan, not minted, precisely for this."""
        first, _ = _compile()
        second = compile_plan(
            first.draft,
            {"version": 1, "brief": {}, "beats": [], "directives": [_shot()]},
            context=build_context(
                segments=SEGMENTS, keep_ranges=[], source_duration=20.0, aspect="16:9"
            ),
            assets_by_directive={"d1": _asset(1)},
            plan_id=7,
            applied_at="2026-08-11T00:00:00Z",
        )
        self.assertEqual(len(second.draft["timelineMediaItems"]), 1)

    def test_the_run_stamps_itself_on_the_draft(self) -> None:
        result, _ = _compile()
        self.assertEqual(result.draft["directorPlanId"], 7)
        self.assertEqual(result.draft["directorAppliedAt"], "2026-08-11T00:00:00Z")

    def test_the_manifest_records_everything_created(self) -> None:
        """Revert is a filter over this, not a diff against the user's work."""
        result, _ = _compile()
        self.assertEqual(result.manifest["timelineMediaItemIds"], ["dir7-d1"])
        self.assertEqual(result.manifest["clipAttributeKeys"], ["media:dir7-d1"])


class RevertTests(unittest.TestCase):
    def test_reverting_removes_exactly_what_was_added(self) -> None:
        draft = {
            "timelineMediaItems": [{"id": "mine", "track": "video", "trackId": "track-video-1"}],
            "clipAttributes": {"media:mine": {"mirror": True}},
        }
        result, _ = _compile(draft=draft)
        reverted = revert_plan(result.draft, result.manifest)

        self.assertEqual([i["id"] for i in reverted["timelineMediaItems"]], ["mine"])
        self.assertEqual(list(reverted["clipAttributes"]), ["media:mine"])
        self.assertNotIn("directorPlanId", reverted)

    def test_the_broll_track_goes_too_when_it_is_left_empty(self) -> None:
        result, _ = _compile()
        reverted = revert_plan(result.draft, result.manifest)
        self.assertNotIn("V2", [t["label"] for t in reverted["timelineTracks"]])

    def test_the_broll_track_stays_if_the_user_put_something_there(self) -> None:
        """Reverting the director's work must not take the user's with it."""
        result, _ = _compile()
        track_id = result.manifest["trackIds"][0]
        result.draft["timelineMediaItems"].append(
            {"id": "mine", "track": "video", "trackId": track_id}
        )
        reverted = revert_plan(result.draft, result.manifest)
        self.assertIn("V2", [t["label"] for t in reverted["timelineTracks"]])

    def test_reverting_twice_is_harmless(self) -> None:
        result, _ = _compile()
        once = revert_plan(result.draft, result.manifest)
        twice = revert_plan(once, result.manifest)
        self.assertEqual(once["timelineMediaItems"], twice["timelineMediaItems"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
