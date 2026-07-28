import unittest
from unittest import mock

from app.api.routes.ai import _analyze_segments, _summarize_analysis


class AnalyzeSegmentsTests(unittest.TestCase):
    def test_detects_filler_silence_and_bad_take(self) -> None:
        segments = [
            {"start": 0.0, "end": 2.0, "text": "Hello everyone um welcome back"},
            # 1.0s silence gap before the next segment (> 0.65 threshold)
            {"start": 3.0, "end": 5.0, "text": "Today we talk about editing"},
            {"start": 5.0, "end": 7.0, "text": "scratch that let me restart"},
        ]
        analysis = _analyze_segments(segments, duration=7.0)

        kinds = {s["kind"] for s in analysis["suggestions"]}
        self.assertIn("filler", kinds)
        self.assertIn("silence", kinds)
        self.assertIn("bad_take", kinds)
        # keepRanges are the non-bad-take segments, merged.
        self.assertTrue(analysis["keepRanges"])
        self.assertTrue(all(r["end"] > r["start"] for r in analysis["keepRanges"]))

    def test_empty_segments(self) -> None:
        analysis = _analyze_segments([], duration=0.0)
        self.assertEqual(analysis["keepRanges"], [])
        self.assertEqual(analysis["suggestions"], [])

    def test_default_params_match_old_behavior(self) -> None:
        """Calling with no keyword args (today's call sites) must equal calling
        with the new options all at their defaults — full back-compat."""
        segments = [
            {"start": 0.0, "end": 2.0, "text": "Hello everyone um welcome back"},
            {"start": 3.0, "end": 5.0, "text": "Today we talk about editing"},
            {"start": 5.0, "end": 7.0, "text": "scratch that let me restart"},
        ]
        legacy = _analyze_segments(segments, duration=7.0)
        explicit_defaults = _analyze_segments(
            segments,
            duration=7.0,
            remove_fillers=True,
            remove_silences=True,
            remove_bad_takes=True,
            aggressiveness="balanced",
        )
        self.assertEqual(legacy, explicit_defaults)


class AggressivenessSilenceThresholdTests(unittest.TestCase):
    """A 0.5s gap and a 0.8s gap should be classified differently at each
    aggressiveness level: light=0.9 (both kept, no suggestion), balanced=0.65
    (only the 0.8s gap flagged), aggressive=0.4 (both gaps flagged)."""

    def _segments_with_gaps(self):
        return [
            {"start": 0.0, "end": 1.0, "text": "one"},
            # 0.5s gap
            {"start": 1.5, "end": 2.5, "text": "two"},
            # 0.8s gap
            {"start": 3.3, "end": 4.3, "text": "three"},
        ]

    def _silence_gaps(self, analysis):
        return sorted(
            round(float(s["detail"].rstrip("s")), 1)
            for s in analysis["suggestions"]
            if s["kind"] == "silence"
        )

    def test_light_flags_neither_gap(self) -> None:
        analysis = _analyze_segments(
            self._segments_with_gaps(), duration=4.3, aggressiveness="light"
        )
        self.assertEqual(self._silence_gaps(analysis), [])

    def test_balanced_flags_only_larger_gap(self) -> None:
        analysis = _analyze_segments(
            self._segments_with_gaps(), duration=4.3, aggressiveness="balanced"
        )
        self.assertEqual(self._silence_gaps(analysis), [0.8])

    def test_aggressive_flags_both_gaps(self) -> None:
        analysis = _analyze_segments(
            self._segments_with_gaps(), duration=4.3, aggressiveness="aggressive"
        )
        self.assertEqual(self._silence_gaps(analysis), [0.5, 0.8])


class CategoryFlagTests(unittest.TestCase):
    def _segments(self):
        return [
            {"start": 0.0, "end": 2.0, "text": "Hello everyone um welcome back"},
            # 1.0s silence gap before the next segment (> 0.65 threshold)
            {"start": 3.0, "end": 5.0, "text": "Today we talk about editing"},
            {"start": 5.0, "end": 7.0, "text": "scratch that let me restart"},
        ]

    def test_remove_fillers_false_drops_filler_suggestions_only(self) -> None:
        baseline = _analyze_segments(self._segments(), duration=7.0)
        analysis = _analyze_segments(self._segments(), duration=7.0, remove_fillers=False)

        kinds = {s["kind"] for s in analysis["suggestions"]}
        self.assertNotIn("filler", kinds)
        self.assertIn("silence", kinds)
        self.assertIn("bad_take", kinds)
        # Disabling a category must not change keepRanges.
        self.assertEqual(analysis["keepRanges"], baseline["keepRanges"])

    def test_remove_silences_false_drops_silence_suggestions_only(self) -> None:
        baseline = _analyze_segments(self._segments(), duration=7.0)
        analysis = _analyze_segments(self._segments(), duration=7.0, remove_silences=False)

        kinds = {s["kind"] for s in analysis["suggestions"]}
        self.assertNotIn("silence", kinds)
        self.assertIn("filler", kinds)
        self.assertIn("bad_take", kinds)
        self.assertEqual(analysis["keepRanges"], baseline["keepRanges"])

    def test_remove_bad_takes_false_drops_bad_take_suggestions_and_keeps_segment(self) -> None:
        analysis = _analyze_segments(self._segments(), duration=7.0, remove_bad_takes=False)

        kinds = {s["kind"] for s in analysis["suggestions"]}
        self.assertNotIn("bad_take", kinds)
        self.assertIn("filler", kinds)
        self.assertIn("silence", kinds)
        # keepRanges must NOT be reduced by the disabled bad-take category —
        # the "scratch that let me restart" segment (5.0-7.0) is now kept.
        self.assertTrue(any(r["start"] <= 5.0 and r["end"] >= 7.0 for r in analysis["keepRanges"]))

    def test_all_categories_off_yields_no_suggestions_and_full_keep(self) -> None:
        segments = self._segments()
        analysis = _analyze_segments(
            segments,
            duration=7.0,
            remove_fillers=False,
            remove_silences=False,
            remove_bad_takes=False,
        )
        self.assertEqual(analysis["suggestions"], [])
        # Every segment (including the "bad take" one) is kept — the 1.0s gap
        # between segment 1 and 2 is real dead air, not a merge artifact, so
        # keepRanges is [0,2] + [3,7] (merged 3-5 and 5-7) = 2.0 + 4.0 = 6.0s.
        total_span = sum(r["end"] - r["start"] for r in analysis["keepRanges"])
        self.assertAlmostEqual(total_span, 6.0, places=3)


class RepetitionDetectionTests(unittest.TestCase):
    """Repeated words, phrases and whole sentences — the "repetitive flow" pass."""

    def test_detects_a_repeated_phrase_using_word_timestamps(self) -> None:
        seg = {
            "start": 0.0,
            "end": 3.5,
            "text": "I want to I want to build this",
            "words": [
                {"word": "I", "start": 0.0, "end": 0.3},
                {"word": "want", "start": 0.3, "end": 0.6},
                {"word": "to", "start": 0.6, "end": 0.9},
                {"word": "I", "start": 1.2, "end": 1.5},
                {"word": "want", "start": 1.5, "end": 1.8},
                {"word": "to", "start": 1.8, "end": 2.1},
                {"word": "build", "start": 2.1, "end": 2.6},
                {"word": "this", "start": 2.6, "end": 3.0},
            ],
        }
        analysis = _analyze_segments([seg], duration=3.5)
        repeats = [s for s in analysis["suggestions"] if s["kind"] == "repeat"]
        self.assertEqual(len(repeats), 1)
        # The *first* utterance is dropped; the second leads into "build this".
        self.assertAlmostEqual(repeats[0]["start"], 0.0, places=3)
        self.assertAlmostEqual(repeats[0]["end"], 0.9, places=3)
        self.assertEqual(repeats[0]["detail"], "I want to")

    def test_detects_a_repeated_word(self) -> None:
        seg = {"start": 0.0, "end": 2.0, "text": "the the cat sat down"}
        repeats = [
            s for s in _analyze_segments([seg], duration=2.0)["suggestions"] if s["kind"] == "repeat"
        ]
        self.assertEqual(len(repeats), 1)
        self.assertEqual(repeats[0]["detail"], "the")

    def test_drops_the_earlier_of_two_identical_sentences(self) -> None:
        segments = [
            {"start": 0.0, "end": 2.0, "text": "This is the important part."},
            {"start": 2.0, "end": 4.0, "text": "This is the important part."},
            {"start": 4.0, "end": 6.0, "text": "And then we move on."},
        ]
        analysis = _analyze_segments(segments, duration=6.0)
        repeats = [s for s in analysis["suggestions"] if s["kind"] == "repeat"]
        self.assertEqual([r["title"] for r in repeats], ["Repeated sentence"])
        self.assertAlmostEqual(repeats[0]["start"], 0.0, places=3)
        # The earlier copy is gone from keepRanges; the later one survives.
        self.assertTrue(all(r["start"] >= 2.0 for r in analysis["keepRanges"]))

    def test_ignores_a_short_repeated_interjection(self) -> None:
        segments = [
            {"start": 0.0, "end": 1.0, "text": "Right."},
            {"start": 1.0, "end": 2.0, "text": "Right."},
        ]
        analysis = _analyze_segments(segments, duration=2.0)
        self.assertEqual([s for s in analysis["suggestions"] if s["kind"] == "repeat"], [])

    def test_remove_repeats_false_suppresses_the_whole_category(self) -> None:
        segments = [
            {"start": 0.0, "end": 2.0, "text": "the the cat sat down"},
            {"start": 2.0, "end": 4.0, "text": "This is the important part."},
            {"start": 4.0, "end": 6.0, "text": "This is the important part."},
        ]
        analysis = _analyze_segments(segments, duration=6.0, remove_repeats=False)
        self.assertEqual([s for s in analysis["suggestions"] if s["kind"] == "repeat"], [])
        # ...and nothing is dropped from keepRanges either.
        self.assertAlmostEqual(sum(r["end"] - r["start"] for r in analysis["keepRanges"]), 6.0, places=3)

    def test_does_not_scan_a_segment_it_already_dropped(self) -> None:
        segments = [{"start": 0.0, "end": 2.0, "text": "no no scratch that let me restart"}]
        analysis = _analyze_segments(segments, duration=2.0)
        kinds = [s["kind"] for s in analysis["suggestions"]]
        self.assertIn("bad_take", kinds)
        self.assertNotIn("repeat", kinds)


class SummarizeAnalysisTests(unittest.TestCase):
    def test_counts_and_removable_seconds(self) -> None:
        analysis = {
            "suggestions": [
                {"kind": "filler", "start": 1.0, "end": 1.3},
                {"kind": "filler", "start": 2.0, "end": 2.4},
                {"kind": "silence", "start": 3.0, "end": 4.0},
                {"kind": "bad_take", "start": 5.0, "end": 7.0},
                {"kind": "repeat", "start": 7.0, "end": 7.5},
                {"kind": "broll", "start": 8.0, "end": 9.0},  # ignored
            ]
        }
        counts, removable = _summarize_analysis(analysis)
        self.assertEqual(counts, {"fillers": 2, "silences": 1, "bad_takes": 1, "repeats": 1})
        # 0.3 + 0.4 + 1.0 + 2.0 + 0.5 = 4.2 (broll excluded)
        self.assertAlmostEqual(removable, 4.2, places=5)

    def test_handles_missing_suggestions_key(self) -> None:
        counts, removable = _summarize_analysis({})
        self.assertEqual(counts, {"fillers": 0, "silences": 0, "bad_takes": 0, "repeats": 0})
        self.assertEqual(removable, 0.0)


class RoughCutEndpointOptionsTests(unittest.TestCase):
    """POST /videos/{id}/ai/rough-cut with an AutoEditOptions body parses and
    filters — using an in-memory sqlite DB per existing route-level test
    patterns (see test_repurpose_params_and_source_videos.py)."""

    def setUp(self) -> None:
        from sqlalchemy import create_engine
        from sqlalchemy.dialects.postgresql import JSONB
        from sqlalchemy.ext.compiler import compiles
        from sqlalchemy.orm import sessionmaker

        from app.db.database import Base
        from app.db.models import AiResult, Comment, Project, User, Video, VideoTranscription

        if not getattr(JSONB, "_b3_sqlite_compiled", False):
            @compiles(JSONB, "sqlite")
            def _compile_jsonb_for_sqlite(type_, compiler, **kw):
                return "JSON"

            JSONB._b3_sqlite_compiled = True

        engine = create_engine("sqlite://")
        tables = [
            User.__table__,
            Project.__table__,
            Video.__table__,
            VideoTranscription.__table__,
            AiResult.__table__,
            Comment.__table__,
        ]
        Base.metadata.create_all(engine, tables=tables)
        self.db = sessionmaker(bind=engine)()

        self.user = User(email="editor@example.com", name="Edna Editor", role="creator")
        self.db.add(self.user)
        self.db.flush()

        self.project = Project(name="Launch", creator_id=self.user.id, workspace_id=1)
        self.db.add(self.project)
        self.db.flush()

        self.video = Video(
            project_id=self.project.id,
            name="Source",
            version=1,
            file_path="https://example.test/source.mp4",
            uploader_id=self.user.id,
            duration=7,
        )
        self.db.add(self.video)
        self.db.flush()

        self.transcription = VideoTranscription(
            video_id=self.video.id,
            status="completed",
            segments=[
                {"start": 0.0, "end": 2.0, "text": "Hello everyone um welcome back"},
                {"start": 3.0, "end": 5.0, "text": "Today we talk about editing"},
                {"start": 5.0, "end": 7.0, "text": "scratch that let me restart"},
            ],
        )
        self.db.add(self.transcription)
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    def test_rough_cut_with_options_body_filters_categories(self) -> None:
        from app.api.routes import ai as ai_routes

        with mock.patch.object(ai_routes, "can_access_project", return_value=True):
            result = ai_routes.rough_cut(
                video_id=self.video.id,
                body=ai_routes.RoughCutRequest(
                    remove_fillers=False,
                    remove_silences=False,
                    remove_bad_takes=True,
                    aggressiveness="aggressive",
                ),
                db=self.db,
                current_user=self.user,
            )

        data = result["result_data"]
        kinds = {s["kind"] for s in data["suggestions"]}
        self.assertNotIn("filler", kinds)
        self.assertNotIn("silence", kinds)
        self.assertTrue(data["keepRanges"])

    def test_rough_cut_default_body_matches_legacy_behavior(self) -> None:
        from app.api.routes import ai as ai_routes
        from app.api.routes.ai import _analyze_segments

        with mock.patch.object(ai_routes, "can_access_project", return_value=True):
            result = ai_routes.rough_cut(
                video_id=self.video.id,
                body=ai_routes.RoughCutRequest(),
                db=self.db,
                current_user=self.user,
            )

        expected = _analyze_segments(list(self.transcription.segments), duration=7.0)
        data = result["result_data"]
        self.assertEqual(data["keepRanges"], expected["keepRanges"])
        self.assertEqual(data["suggestions"], expected["suggestions"])

    def test_review_endpoint_absent_body_matches_legacy_behavior(self) -> None:
        from app.api.routes import ai as ai_routes

        with mock.patch.object(ai_routes, "can_access_project", return_value=True), mock.patch.object(
            ai_routes, "generate_json", return_value={"engagement_score": 80}
        ):
            result = ai_routes.review_video(
                video_id=self.video.id,
                body=None,
                db=self.db,
                current_user=self.user,
            )

        data = result["result_data"]
        self.assertEqual(data["counts"], {"fillers": 1, "silences": 1, "bad_takes": 1, "repeats": 0})

    def test_review_endpoint_with_options_body_filters_counts(self) -> None:
        from app.api.routes import ai as ai_routes

        with mock.patch.object(ai_routes, "can_access_project", return_value=True), mock.patch.object(
            ai_routes, "generate_json", return_value={"engagement_score": 80}
        ):
            result = ai_routes.review_video(
                video_id=self.video.id,
                body=ai_routes.AutoEditOptions(remove_bad_takes=False),
                db=self.db,
                current_user=self.user,
            )

        data = result["result_data"]
        self.assertEqual(data["counts"], {"fillers": 1, "silences": 1, "bad_takes": 0, "repeats": 0})


if __name__ == "__main__":
    unittest.main()
