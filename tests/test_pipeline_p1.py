"""Task P1: server pipeline (transcribe -> analyze -> seed cuts -> clips) +
pipeline status endpoint.

Covers:
- PUT/GET /videos/{id}/ai/auto-edit-prefs roundtrip.
- app.services.auto_edit.run_post_transcription_auto_edit: seeds/merges the
  rough_cut_draft AiResult (keepRanges gated by auto_apply + no existing user
  draft; aiAnalysis always written when enabled); no-ops when disabled/absent.
- app.services.auto_edit.filter_segments_to_ranges pure behavior, plus its
  integration into repurpose_pipeline.create_clips_for_repurpose_job (clip
  suggestion windows exclude ranges cut by a rough-cut draft).
- GET /projects/{id}/pipeline states: no transcript / pending / completed
  +analyzed / clips partial.
"""

from __future__ import annotations

import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import (
    AiResult,
    Annotation,
    Clip,
    ClipStyle,
    ClipTemplate,
    Comment,
    Folder,
    Project,
    RepurposeJob,
    User,
    Video,
    VideoTranscription,
)

if not getattr(JSONB, "_p1_sqlite_compiled", False):
    @compiles(JSONB, "sqlite")
    def _compile_jsonb_for_sqlite(type_, compiler, **kw):
        return "JSON"

    JSONB._p1_sqlite_compiled = True


class _SqliteDbTestCase(unittest.TestCase):
    """Shared in-memory sqlite fixture covering every table this task touches."""

    tables = [
        User.__table__,
        Project.__table__,
        Video.__table__,
        VideoTranscription.__table__,
        Comment.__table__,
        Annotation.__table__,
        Folder.__table__,
        RepurposeJob.__table__,
        Clip.__table__,
        ClipStyle.__table__,
        ClipTemplate.__table__,
        AiResult.__table__,
    ]

    def setUp(self) -> None:
        engine = create_engine("sqlite://").execution_options(
            schema_translate_map={"repurpose": None}
        )
        Base.metadata.create_all(engine, tables=self.tables)
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
            duration=10,
        )
        self.db.add(self.video)
        self.db.flush()

    def tearDown(self) -> None:
        self.db.close()

    def _ai_result(self, result_type: str) -> AiResult | None:
        return (
            self.db.query(AiResult)
            .filter(AiResult.video_id == self.video.id, AiResult.result_type == result_type)
            .first()
        )


# --- 1. Auto-edit prefs endpoint roundtrip ---------------------------------


class AutoEditPrefsEndpointTests(_SqliteDbTestCase):
    def test_put_then_get_roundtrip(self):
        from app.api.routes import ai as ai_routes

        with mock.patch.object(ai_routes, "can_access_project", return_value=True):
            put_result = ai_routes.save_auto_edit_prefs(
                video_id=self.video.id,
                body=ai_routes.AutoEditPrefsBody(
                    enabled=True,
                    auto_apply=True,
                    remove_fillers=True,
                    remove_silences=False,
                    remove_bad_takes=True,
                    aggressiveness="aggressive",
                ),
                db=self.db,
                current_user=self.user,
            )
            get_result = ai_routes.get_auto_edit_prefs(
                video_id=self.video.id, db=self.db, current_user=self.user
            )

        self.assertEqual(put_result["result_data"]["enabled"], True)
        self.assertEqual(put_result["result_data"]["auto_apply"], True)
        self.assertEqual(get_result["result_data"], put_result["result_data"])
        self.assertEqual(get_result["result_data"]["aggressiveness"], "aggressive")
        self.assertEqual(get_result["result_data"]["remove_silences"], False)

    def test_get_defaults_when_absent(self):
        from app.api.routes import ai as ai_routes

        with mock.patch.object(ai_routes, "can_access_project", return_value=True):
            result = ai_routes.get_auto_edit_prefs(
                video_id=self.video.id, db=self.db, current_user=self.user
            )

        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["result_data"]["enabled"], False)
        self.assertEqual(result["result_data"]["auto_apply"], False)


# --- 2. Post-transcription auto-edit hook ----------------------------------


_SEGMENTS = [
    {"start": 0.0, "end": 2.0, "text": "Hello everyone um welcome back"},
    # 1.0s silence gap
    {"start": 3.0, "end": 5.0, "text": "Today we talk about editing"},
    {"start": 5.0, "end": 7.0, "text": "scratch that let me restart"},
]


class PostTranscriptionAutoEditHookTests(_SqliteDbTestCase):
    def _set_prefs(self, **kwargs) -> None:
        defaults = {
            "enabled": True,
            "auto_apply": True,
            "remove_fillers": True,
            "remove_silences": True,
            "remove_bad_takes": True,
            "aggressiveness": "balanced",
        }
        defaults.update(kwargs)
        self.db.add(AiResult(video_id=self.video.id, result_type="auto_edit_prefs", result_data=defaults))
        self.db.commit()

    def test_noop_when_no_prefs_row(self):
        from app.services.auto_edit import run_post_transcription_auto_edit

        run_post_transcription_auto_edit(
            self.db, self.video.id, segments=_SEGMENTS, video_duration=7.0, transcription_id=1
        )
        self.assertIsNone(self._ai_result("rough_cut_draft"))

    def test_noop_when_prefs_disabled(self):
        from app.services.auto_edit import run_post_transcription_auto_edit

        self._set_prefs(enabled=False)
        run_post_transcription_auto_edit(
            self.db, self.video.id, segments=_SEGMENTS, video_duration=7.0, transcription_id=1
        )
        self.assertIsNone(self._ai_result("rough_cut_draft"))

    def test_auto_apply_true_seeds_keep_ranges_and_ai_analysis(self):
        from app.services.auto_edit import run_post_transcription_auto_edit

        self._set_prefs(enabled=True, auto_apply=True)
        run_post_transcription_auto_edit(
            self.db, self.video.id, segments=_SEGMENTS, video_duration=7.0, transcription_id=42
        )
        draft = self._ai_result("rough_cut_draft")
        self.assertIsNotNone(draft)
        data = draft.result_data
        self.assertTrue(data["keepRanges"])
        self.assertEqual(data["aiAnalysis"]["analyzedAt"], "transcription:42")
        self.assertIn("suggestions", data["aiAnalysis"])
        self.assertEqual(
            data["aiAnalysis"]["counts"], {"fillers": 1, "silences": 1, "bad_takes": 1, "repeats": 0}
        )
        self.assertEqual(data["aiAnalysis"]["options"]["aggressiveness"], "balanced")

    def test_auto_apply_false_only_writes_ai_analysis_not_keep_ranges(self):
        from app.services.auto_edit import run_post_transcription_auto_edit

        self._set_prefs(enabled=True, auto_apply=False)
        run_post_transcription_auto_edit(
            self.db, self.video.id, segments=_SEGMENTS, video_duration=7.0, transcription_id=1
        )
        draft = self._ai_result("rough_cut_draft")
        self.assertIsNotNone(draft)
        data = draft.result_data
        self.assertNotIn("keepRanges", data)
        self.assertIn("aiAnalysis", data)

    def test_selected_source_range_constrains_auto_edit_keep_ranges(self):
        from app.services.auto_edit import run_post_transcription_auto_edit

        self._set_prefs(
            enabled=True,
            auto_apply=True,
            source_range_start_seconds=3.0,
            source_range_end_seconds=5.5,
        )
        run_post_transcription_auto_edit(
            self.db,
            self.video.id,
            segments=_SEGMENTS,
            video_duration=7.0,
            transcription_id=1,
        )

        ranges = self._ai_result("rough_cut_draft").result_data["keepRanges"]
        self.assertTrue(ranges)
        self.assertTrue(all(item["start"] >= 3.0 for item in ranges))
        self.assertTrue(all(item["end"] <= 5.5 for item in ranges))

    def test_existing_user_draft_with_range_edit_version_is_never_clobbered(self):
        from app.services.auto_edit import run_post_transcription_auto_edit

        self._set_prefs(enabled=True, auto_apply=True)
        user_keep_ranges = [{"start": 0.0, "end": 1.0}]
        self.db.add(
            AiResult(
                video_id=self.video.id,
                result_type="rough_cut_draft",
                result_data={
                    "keepRanges": user_keep_ranges,
                    "rangeEditVersion": 3,
                    "segments": ["user-edited-marker"],
                },
            )
        )
        self.db.commit()

        run_post_transcription_auto_edit(
            self.db, self.video.id, segments=_SEGMENTS, video_duration=7.0, transcription_id=1
        )
        draft = self._ai_result("rough_cut_draft")
        data = draft.result_data
        # keepRanges untouched (still the user's, not the auto-analysis's).
        self.assertEqual(data["keepRanges"], user_keep_ranges)
        # Other pre-existing user fields are preserved (merge, not overwrite).
        self.assertEqual(data["segments"], ["user-edited-marker"])
        # aiAnalysis is still populated so the Edit tab can show suggestions.
        self.assertIn("aiAnalysis", data)

    def test_hook_never_raises_on_analysis_error(self):
        from app.services.auto_edit import run_post_transcription_auto_edit

        self._set_prefs(enabled=True, auto_apply=True)
        with mock.patch(
            "app.services.auto_edit._analyze_segments", side_effect=RuntimeError("boom")
        ):
            try:
                run_post_transcription_auto_edit(
                    self.db, self.video.id, segments=_SEGMENTS, video_duration=7.0, transcription_id=1
                )
            except Exception as exc:  # pragma: no cover - the assertion below is the real check
                self.fail(f"hook must never raise, got {exc!r}")


# --- 3. filter_segments_to_ranges + repurpose_pipeline integration --------


class FilterSegmentsToRangesTests(unittest.TestCase):
    def test_no_ranges_returns_segments_unchanged(self):
        from app.services.auto_edit import filter_segments_to_ranges

        segments = [{"start": 0.0, "end": 2.0, "text": "a"}]
        self.assertEqual(filter_segments_to_ranges(segments, []), segments)

    def test_clips_segment_to_overlap_and_drops_excluded_segment(self):
        from app.services.auto_edit import filter_segments_to_ranges

        segments = [
            {"start": 0.0, "end": 2.0, "text": "kept", "speaker": "SPEAKER_1"},
            {"start": 3.0, "end": 4.0, "text": "cut-away entirely"},
            {"start": 5.0, "end": 9.0, "text": "partially kept"},
        ]
        ranges = [{"start": 0.0, "end": 2.0}, {"start": 6.0, "end": 9.0}]
        out = filter_segments_to_ranges(segments, ranges)

        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["start"], 0.0)
        self.assertEqual(out[0]["end"], 2.0)
        self.assertEqual(out[0]["speaker"], "SPEAKER_1")
        self.assertEqual(out[1]["start"], 6.0)
        self.assertEqual(out[1]["end"], 9.0)
        self.assertEqual(out[1]["text"], "partially kept")


def _fake_suggestion(start: float, end: float):
    from app.services.clip_analysis import ClipSuggestion

    return ClipSuggestion(
        start_time=start,
        end_time=end,
        duration=end - start,
        virality_score=0.8,
        reason="test",
        transcript=f"segment from {start} to {end}",
        hooks_matched=["hook"],
    )


class RepurposePipelineFiltersToKeepRangesTests(_SqliteDbTestCase):
    def _make_job(self) -> RepurposeJob:
        job = RepurposeJob(
            user_id=self.user.id,
            project_id=self.project.id,
            video_id=self.video.id,
            source_mode="project_video",
            clip_mode="basic",
            clip_length_bucket="lt_30",
            aspect_ratio="9:16",
            status="processing",
        )
        self.db.add(job)
        self.db.flush()
        vt = VideoTranscription(
            video_id=self.video.id,
            status="completed",
            segments=[
                {"start": 0.0, "end": 2.0, "text": "kept"},
                {"start": 3.0, "end": 4.0, "text": "cut away"},
                {"start": 5.0, "end": 10.0, "text": "also kept"},
            ],
        )
        self.db.add(vt)
        self.db.commit()
        return job

    def _run_pipeline(self, job: RepurposeJob):
        from app.services.repurpose_pipeline import create_clips_for_repurpose_job

        with mock.patch(
            "app.services.repurpose_pipeline.suggest_clips",
            return_value=[_fake_suggestion(0, 2)],
        ) as suggest_mock, mock.patch(
            "app.jobs.queue.enqueue_clip_render_job", return_value=None
        ), mock.patch(
            "app.services.clip_renderer.fast_thumbnail_for_clip", return_value=None
        ):
            create_clips_for_repurpose_job(self.db, job.id, video_duration=10.0)
        return suggest_mock

    def test_no_draft_leaves_segments_unchanged(self):
        job = self._make_job()
        suggest_mock = self._run_pipeline(job)
        passed_segments = suggest_mock.call_args.args[0]
        self.assertEqual(len(passed_segments), 3)

    def test_trivial_full_duration_range_leaves_segments_unchanged(self):
        job = self._make_job()
        self.db.add(
            AiResult(
                video_id=self.video.id,
                result_type="rough_cut_draft",
                result_data={"keepRanges": [{"start": 0.0, "end": 10.0}]},
            )
        )
        self.db.commit()
        suggest_mock = self._run_pipeline(job)
        passed_segments = suggest_mock.call_args.args[0]
        self.assertEqual(len(passed_segments), 3)

    def test_non_trivial_draft_ranges_filter_out_cut_segment(self):
        job = self._make_job()
        self.db.add(
            AiResult(
                video_id=self.video.id,
                result_type="rough_cut_draft",
                result_data={"keepRanges": [{"start": 0.0, "end": 2.0}, {"start": 5.0, "end": 10.0}]},
            )
        )
        self.db.commit()
        suggest_mock = self._run_pipeline(job)
        passed_segments = suggest_mock.call_args.args[0]

        texts = {s["text"] for s in passed_segments}
        self.assertEqual(texts, {"kept", "also kept"})
        self.assertNotIn("cut away", texts)

    def test_saved_ranges_are_used_when_video_duration_is_unknown(self):
        job = self._make_job()
        self.video.duration = None
        self.db.add(
            AiResult(
                video_id=self.video.id,
                result_type="rough_cut_draft",
                result_data={"keepRanges": [{"start": 5.0, "end": 10.0}]},
            )
        )
        self.db.commit()

        from app.services.repurpose_pipeline import create_clips_for_repurpose_job

        with mock.patch(
            "app.services.repurpose_pipeline.suggest_clips",
            return_value=[_fake_suggestion(5, 10)],
        ) as suggest_mock, mock.patch(
            "app.jobs.queue.enqueue_clip_render_job", return_value=None
        ), mock.patch(
            "app.services.clip_renderer.fast_thumbnail_for_clip", return_value=None
        ):
            create_clips_for_repurpose_job(self.db, job.id)

        passed_segments = suggest_mock.call_args.args[0]
        self.assertEqual({segment["text"] for segment in passed_segments}, {"also kept"})


# --- 4. GET /projects/{id}/pipeline ----------------------------------------


class ProjectPipelineEndpointTests(_SqliteDbTestCase):
    def _call(self, video_id=None):
        from app.api.routes.projects import get_project_pipeline

        return get_project_pipeline(
            project_id=self.project.id,
            video_id=video_id,
            db=self.db,
            current_user=self.user,
        )

    def test_no_transcript_no_prefs_no_jobs(self):
        result = self._call()
        self.assertEqual(result["transcription"], "none")
        self.assertEqual(result["analysis"], "none")
        self.assertEqual(result["clips"], {"state": "none", "ready": 0, "total": 0})

    def test_pending_transcription_with_enabled_prefs_marks_analysis_pending(self):
        self.db.add(VideoTranscription(video_id=self.video.id, status="pending"))
        self.db.add(
            AiResult(
                video_id=self.video.id,
                result_type="auto_edit_prefs",
                result_data={"enabled": True, "auto_apply": True},
            )
        )
        self.db.commit()

        result = self._call()
        self.assertEqual(result["transcription"], "pending")
        self.assertEqual(result["analysis"], "pending")
        self.assertEqual(result["clips"]["state"], "none")

    def test_completed_and_analyzed(self):
        self.db.add(VideoTranscription(video_id=self.video.id, status="completed", segments=[]))
        self.db.add(
            AiResult(
                video_id=self.video.id,
                result_type="rough_cut_draft",
                result_data={
                    "keepRanges": [{"start": 0.0, "end": 5.0}],
                    "aiAnalysis": {"suggestions": [], "counts": {}, "options": {}, "analyzedAt": "transcription:1"},
                },
            )
        )
        self.db.commit()

        result = self._call()
        self.assertEqual(result["transcription"], "completed")
        self.assertEqual(result["analysis"], "done")

    def test_clips_partial_state(self):
        self.db.add(VideoTranscription(video_id=self.video.id, status="completed", segments=[]))
        job = RepurposeJob(
            user_id=self.user.id,
            project_id=self.project.id,
            video_id=self.video.id,
            source_mode="project_video",
            clip_mode="basic",
            clip_length_bucket="lt_30",
            aspect_ratio="9:16",
            status="completed",
        )
        self.db.add(job)
        self.db.flush()
        self.db.add(
            Clip(
                video_id=self.video.id,
                user_id=self.user.id,
                start_time=0.0,
                end_time=5.0,
                duration_seconds=5.0,
                status="ready",
            )
        )
        self.db.add(
            Clip(
                video_id=self.video.id,
                user_id=self.user.id,
                start_time=5.0,
                end_time=10.0,
                duration_seconds=5.0,
                status="queued",
            )
        )
        self.db.commit()

        result = self._call()
        self.assertEqual(result["clips"], {"state": "generating", "ready": 1, "total": 2})

    def test_clips_done_when_all_ready(self):
        job = RepurposeJob(
            user_id=self.user.id,
            project_id=self.project.id,
            video_id=self.video.id,
            source_mode="project_video",
            clip_mode="basic",
            clip_length_bucket="lt_30",
            aspect_ratio="9:16",
            status="completed",
        )
        self.db.add(job)
        self.db.flush()
        self.db.add(
            Clip(
                video_id=self.video.id,
                user_id=self.user.id,
                start_time=0.0,
                end_time=5.0,
                duration_seconds=5.0,
                status="ready",
            )
        )
        self.db.commit()

        result = self._call()
        self.assertEqual(result["clips"], {"state": "done", "ready": 1, "total": 1})

    def test_failed_transcription_forces_analysis_none_even_if_prefs_enabled(self):
        self.db.add(VideoTranscription(video_id=self.video.id, status="failed"))
        self.db.add(
            AiResult(
                video_id=self.video.id,
                result_type="auto_edit_prefs",
                result_data={"enabled": True, "auto_apply": True},
            )
        )
        self.db.commit()

        result = self._call()
        self.assertEqual(result["transcription"], "failed")
        # Would be "pending" (auto_edit_enabled, no analysis yet) if not for
        # the failed-transcription gate — a failed transcription will never
        # produce analysis, so the strip must not keep polling/spinning.
        self.assertEqual(result["analysis"], "none")

    def test_failed_transcription_keeps_prior_completed_analysis_done(self):
        self.db.add(VideoTranscription(video_id=self.video.id, status="failed"))
        self.db.add(
            AiResult(
                video_id=self.video.id,
                result_type="rough_cut_draft",
                result_data={
                    "keepRanges": [{"start": 0.0, "end": 5.0}],
                    "aiAnalysis": {"suggestions": [], "counts": {}, "options": {}, "analyzedAt": "transcription:1"},
                },
            )
        )
        self.db.commit()

        result = self._call()
        self.assertEqual(result["transcription"], "failed")
        self.assertEqual(result["analysis"], "done")

    def test_client_format_ai_analysis_without_marker_does_not_count_as_done(self):
        self.db.add(VideoTranscription(video_id=self.video.id, status="completed", segments=[]))
        self.db.add(
            AiResult(
                video_id=self.video.id,
                result_type="auto_edit_prefs",
                result_data={"enabled": True, "auto_apply": True},
            )
        )
        self.db.add(
            AiResult(
                video_id=self.video.id,
                result_type="rough_cut_draft",
                result_data={
                    # Shape the rough-cut editor's own draftPayload writes
                    # (rough-cut-draft-state.ts) — no `analyzedAt` marker.
                    "aiAnalysis": {"showFillers": True, "removeSilence": True, "smoothSpeech": True, "suggestions": []},
                },
            )
        )
        self.db.commit()

        result = self._call()
        self.assertEqual(result["transcription"], "completed")
        # Not "done" — this aiAnalysis came from a client save, not the
        # server auto-edit hook, so it must not silently mark analysis done.
        self.assertEqual(result["analysis"], "pending")

    def test_defaults_to_latest_video_when_video_id_omitted(self):
        import datetime

        second = Video(
            project_id=self.project.id,
            name="Second",
            version=2,
            file_path="https://example.test/second.mp4",
            uploader_id=self.user.id,
        )
        self.db.add(second)
        self.db.commit()
        self.db.refresh(second)
        # sqlite CURRENT_TIMESTAMP has second precision, so the two videos
        # created within the same test can tie; force a deterministic order.
        self.video.updated_at = datetime.datetime(2020, 1, 1)
        second.updated_at = datetime.datetime(2020, 1, 2)
        self.db.commit()

        result = self._call()
        self.assertEqual(result["video_id"], second.id)

    def test_explicit_video_id_selects_that_video(self):
        result = self._call(video_id=self.video.id)
        self.assertEqual(result["video_id"], self.video.id)


if __name__ == "__main__":
    unittest.main()
