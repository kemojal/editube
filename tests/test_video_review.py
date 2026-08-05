"""Frame sampling and result hardening for the multimodal AI review."""

import unittest
from pathlib import Path
from unittest import mock

from app.services import review_frames
from app.services.review_frames import extract_frames, frame_budget, pick_timestamps
from app.services.video_review import (
    SCORE_DIMENSIONS,
    empty_review,
    harden_review,
    heuristic_score,
)


class FrameBudgetTests(unittest.TestCase):
    def test_defaults_when_unset(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("AI_REVIEW_FRAME_COUNT", None)
            self.assertEqual(frame_budget(), review_frames.DEFAULT_FRAME_COUNT)

    def test_reads_env_and_clamps(self) -> None:
        with mock.patch.dict("os.environ", {"AI_REVIEW_FRAME_COUNT": "4"}):
            self.assertEqual(frame_budget(), 4)
        with mock.patch.dict("os.environ", {"AI_REVIEW_FRAME_COUNT": "999"}):
            self.assertEqual(frame_budget(), 24)
        with mock.patch.dict("os.environ", {"AI_REVIEW_FRAME_COUNT": "0"}):
            self.assertEqual(frame_budget(), 1)

    def test_garbage_env_falls_back(self) -> None:
        with mock.patch.dict("os.environ", {"AI_REVIEW_FRAME_COUNT": "ten"}):
            self.assertEqual(frame_budget(), review_frames.DEFAULT_FRAME_COUNT)


class PickTimestampsTests(unittest.TestCase):
    def test_zero_duration_yields_nothing(self) -> None:
        self.assertEqual(pick_timestamps(0), [])
        self.assertEqual(pick_timestamps(-5), [])
        self.assertEqual(pick_timestamps(None), [])

    def test_respects_the_budget(self) -> None:
        self.assertLessEqual(len(pick_timestamps(600, limit=6)), 6)

    def test_is_sorted_deduped_and_inside_the_video(self) -> None:
        stamps = pick_timestamps(120, limit=10)
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(len(stamps), len(set(stamps)))
        self.assertTrue(all(0 <= t <= 120 for t in stamps))
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertTrue(all(gap >= 1.0 for gap in gaps), stamps)

    def test_samples_the_hook_densely(self) -> None:
        """Retention is decided in the opening, so at least two frames must land
        in the first 15 seconds of a long video."""
        stamps = pick_timestamps(600, limit=10)
        self.assertGreaterEqual(len([t for t in stamps if t <= 15.0]), 2, stamps)

    def test_anchors_on_detected_bad_takes(self) -> None:
        analysis = {"suggestions": [{"kind": "bad_take", "start": 61.0, "end": 63.0}]}
        stamps = pick_timestamps(120, analysis, limit=10)
        self.assertTrue(any(abs(t - 61.3) < 0.5 for t in stamps), stamps)

    def test_ignores_silence_suggestions(self) -> None:
        """A silent gap has no visual tell, so it is not worth a frame."""
        analysis = {"suggestions": [{"kind": "silence", "start": 61.0, "end": 63.0}]}
        with_silence = pick_timestamps(120, analysis, limit=10)
        without = pick_timestamps(120, None, limit=10)
        self.assertEqual(with_silence, without)

    def test_survives_malformed_suggestions(self) -> None:
        analysis = {"suggestions": [None, "nope", {"kind": "bad_take", "start": "x"}, {}]}
        self.assertTrue(pick_timestamps(60, analysis, limit=5))

    def test_short_video_still_gets_frames(self) -> None:
        stamps = pick_timestamps(3.0, limit=10)
        self.assertTrue(stamps)
        self.assertTrue(all(0 <= t <= 3.0 for t in stamps))


class ExtractFramesTests(unittest.TestCase):
    def test_skips_timestamps_ffmpeg_cannot_reach(self) -> None:
        """A partial filmstrip is still useful — one bad seek must not abort."""
        def fake_extract(src, dst, *, seek):
            if seek == 2.0:
                return False
            Path(dst).write_bytes(b"jpeg")
            return True

        with mock.patch.object(
            review_frames, "generate_thumbnail_to_path", side_effect=fake_extract
        ):
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                frames = extract_frames("video.mp4", [1.0, 2.0, 3.0], Path(tmp))

        self.assertEqual([t for t, _ in frames], [1.0, 3.0])

    def test_empty_source_returns_nothing(self) -> None:
        self.assertEqual(extract_frames("", [1.0], Path("/tmp")), [])


class HeuristicScoreTests(unittest.TestCase):
    def test_clean_video_scores_high(self) -> None:
        self.assertEqual(heuristic_score({"fillers": 0, "silences": 0, "bad_takes": 0}, 600), 100)

    def test_penalties_are_floored_at_35(self) -> None:
        counts = {"fillers": 500, "silences": 500, "bad_takes": 500}
        self.assertEqual(heuristic_score(counts, 60), 35)

    def test_short_video_does_not_divide_by_zero(self) -> None:
        self.assertTrue(0 <= heuristic_score({"fillers": 1}, 0) <= 100)


class HardenReviewTests(unittest.TestCase):
    def _harden(self, raw, frames=None):
        return harden_review(
            raw,
            fallback_score=60,
            frames=frames or [],
            counts={"fillers": 1, "silences": 0, "bad_takes": 0},
            removable_seconds=1.234,
            keep_ranges=[{"start": 0, "end": 5}],
        )

    def test_non_dict_response_falls_back(self) -> None:
        result = self._harden("garbage")
        self.assertEqual(result["engagement_score"], 60)
        self.assertEqual(result["improvements"], [])
        self.assertFalse(result["needs_transcription"])

    def test_score_is_clamped(self) -> None:
        self.assertEqual(self._harden({"engagement_score": 250})["engagement_score"], 100)
        self.assertEqual(self._harden({"engagement_score": -8})["engagement_score"], 0)
        self.assertEqual(self._harden({"engagement_score": "n/a"})["engagement_score"], 60)

    def test_every_dimension_is_present(self) -> None:
        scores = self._harden({"scores": {"hook": 80}})["scores"]
        self.assertEqual(set(scores), set(SCORE_DIMENSIONS))
        self.assertEqual(scores["hook"], 80)
        # An absent dimension inherits the overall score rather than reading 0.
        self.assertEqual(scores["audio"], 60)

    def test_legacy_dimension_object_is_flattened(self) -> None:
        """Rows written before scores became bare numbers must still render."""
        scores = self._harden({"scores": {"pacing": {"score": 42, "note": "old"}}})["scores"]
        self.assertEqual(scores["pacing"], 42)

    def test_notes_are_normalized(self) -> None:
        result = self._harden(
            {
                "improvements": [
                    {
                        "text": "Tighten the intro",
                        "fix": "Cut 0-4s",
                        "start": 0,
                        "end": 4,
                        "severity": "HIGH",
                        "category": "hook",
                    },
                    "a plain string note",
                    {"text": "", "start": 3},
                    {"text": "Odd", "severity": "urgent", "category": "vibes", "start": "x"},
                    None,
                ]
            }
        )
        notes = result["improvements"]
        self.assertEqual(len(notes), 3)
        self.assertEqual(notes[0]["severity"], "high")
        self.assertEqual(notes[1]["text"], "a plain string note")
        self.assertIsNone(notes[1]["start"])
        # Unknown severity/category fall back rather than reaching the UI raw.
        self.assertEqual(notes[2]["severity"], "medium")
        self.assertEqual(notes[2]["category"], "clarity")

    def test_note_end_before_start_is_dropped(self) -> None:
        notes = self._harden({"improvements": [{"text": "x", "start": 10, "end": 4}]})["improvements"]
        self.assertIsNone(notes[0]["end"])

    def test_notes_attach_the_nearest_sampled_frame(self) -> None:
        frames = [{"t": 0.5, "url": "a.jpg"}, {"t": 30.0, "url": "b.jpg"}]
        notes = self._harden(
            {"improvements": [{"text": "late", "start": 28.0}, {"text": "early", "start": 1.0}]},
            frames=frames,
        )["improvements"]
        self.assertEqual(notes[0]["frame_url"], "b.jpg")
        self.assertEqual(notes[1]["frame_url"], "a.jpg")

    def test_notes_have_no_frame_when_none_were_sampled(self) -> None:
        notes = self._harden({"improvements": [{"text": "x", "start": 1.0}]})["improvements"]
        self.assertIsNone(notes[0]["frame_url"])

    def test_notes_carry_no_rationale_field(self) -> None:
        """The report is a note to the editor, not an essay — problem and fix only."""
        notes = self._harden({"improvements": [{"text": "x", "why": "long explanation"}]})[
            "improvements"
        ]
        self.assertNotIn("why", notes[0])
        self.assertEqual(set(notes[0]), {"text", "fix", "start", "end", "severity", "category", "frame_url"})

    def test_prose_blocks_are_gone(self) -> None:
        result = self._harden({"hook": "a paragraph", "pacing": "another paragraph"})
        self.assertNotIn("hook", result)
        self.assertNotIn("pacing", result)

    def test_lists_are_bounded(self) -> None:
        result = self._harden(
            {
                "strengths": [f"s{i}" for i in range(50)],
                "improvements": [{"text": f"n{i}"} for i in range(50)],
                "thumbnail_moments": [{"t": i} for i in range(50)],
            }
        )
        self.assertEqual(len(result["strengths"]), 4)
        self.assertEqual(len(result["improvements"]), 8)
        self.assertEqual(len(result["thumbnail_moments"]), 3)

    def test_blank_strengths_are_dropped(self) -> None:
        self.assertEqual(self._harden({"strengths": ["  ", "", "real"]})["strengths"], ["real"])

    def test_counts_and_ranges_pass_through(self) -> None:
        result = self._harden({})
        self.assertEqual(result["counts"], {"fillers": 1, "silences": 0, "bad_takes": 0})
        self.assertEqual(result["removable_seconds"], 1.2)
        self.assertEqual(result["keepRanges"], [{"start": 0, "end": 5}])


class EmptyReviewTests(unittest.TestCase):
    def test_shape_matches_what_the_panel_reads(self) -> None:
        payload = empty_review("Transcribe first.")
        self.assertTrue(payload["needs_transcription"])
        self.assertIsNone(payload["engagement_score"])
        self.assertEqual(payload["verdict"], "Transcribe first.")
        for key in ("strengths", "improvements", "frames", "thumbnail_moments", "keepRanges"):
            self.assertEqual(payload[key], [], key)


if __name__ == "__main__":
    unittest.main()
