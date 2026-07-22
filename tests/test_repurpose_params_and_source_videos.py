"""Task B2: repurpose job params (clip_count, aspect_ratios fan-out) plus the two
new source-video endpoints (POST /projects/{id}/videos/youtube and
POST /projects/{id}/videos/from-upload).

Covers:
- RepurposeJobCreate validates aspect_ratios against {"9:16","1:1","16:9"} and
  clip_count bounds (1..20).
- create_clips_for_repurpose_job: clip_count controls suggestion count (default
  8 when omitted), aspect_ratios fans each suggested moment out into one Clip
  per aspect ratio, and single aspect_ratio back-compat (no fan-out) still works.
- create_youtube_source_video: resolves stream + metadata (mocked yt-dlp),
  creates the Video row, seeds/enqueues transcription; raises
  YoutubeStreamResolveError on resolve failure; skips the metadata round-trip
  when the caller already supplied thumbnail/duration/name.
- POST /projects/{id}/videos/youtube: happy path + resolve failure -> 422.
- POST /projects/{id}/videos/from-upload: creates video + pending transcription
  seeded with `language`, quota check uses size_bytes.
"""

from __future__ import annotations

import unittest
from unittest import mock

from fastapi import HTTPException
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
from app.services.youtube_stream_resolve import YoutubeStreamResolveError


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(type_, compiler, **kw):
    # Tests run against in-memory SQLite; JSONB columns degrade to plain JSON.
    return "JSON"


class _SqliteDbTestCase(unittest.TestCase):
    """Shared in-memory sqlite setup. `schema_translate_map` maps the
    "repurpose" Postgres schema (used by RepurposeJob/Clip/ClipStyle/
    ClipTemplate) onto SQLite's default schema, since SQLite has no concept of
    Postgres schemas."""

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
        )
        self.db.add(self.video)
        self.db.flush()

    def tearDown(self) -> None:
        self.db.close()


# --- 1. RepurposeJobCreate validation --------------------------------------


class RepurposeJobCreateValidationTests(unittest.TestCase):
    def test_clip_count_defaults_to_none(self):
        from app.api.models.clips import RepurposeJobCreate

        body = RepurposeJobCreate(source_mode="project_video", video_id=1)
        self.assertIsNone(body.clip_count)

    def test_auto_start_defaults_true_and_accepts_deferred(self):
        from app.api.models.clips import RepurposeJobCreate

        immediate = RepurposeJobCreate(source_mode="project_video", video_id=1)
        deferred = RepurposeJobCreate(
            source_mode="project_video", video_id=1, auto_start=False
        )

        self.assertTrue(immediate.auto_start)
        self.assertFalse(deferred.auto_start)

    def test_clip_count_accepts_bounds(self):
        from app.api.models.clips import RepurposeJobCreate

        RepurposeJobCreate(source_mode="project_video", video_id=1, clip_count=1)
        RepurposeJobCreate(source_mode="project_video", video_id=1, clip_count=20)

    def test_clip_count_rejects_out_of_bounds(self):
        from pydantic import ValidationError

        from app.api.models.clips import RepurposeJobCreate

        with self.assertRaises(ValidationError):
            RepurposeJobCreate(source_mode="project_video", video_id=1, clip_count=0)
        with self.assertRaises(ValidationError):
            RepurposeJobCreate(source_mode="project_video", video_id=1, clip_count=21)

    def test_aspect_ratios_accepts_allowed_values(self):
        from app.api.models.clips import RepurposeJobCreate

        body = RepurposeJobCreate(
            source_mode="project_video", video_id=1, aspect_ratios=["9:16", "1:1", "16:9"]
        )
        self.assertEqual(body.aspect_ratios, ["9:16", "1:1", "16:9"])

    def test_aspect_ratios_rejects_invalid_value(self):
        from pydantic import ValidationError

        from app.api.models.clips import RepurposeJobCreate

        with self.assertRaises(ValidationError):
            RepurposeJobCreate(source_mode="project_video", video_id=1, aspect_ratios=["4:5"])


# --- 2. Pipeline: clip_count + aspect_ratios fan-out ------------------------


def _fake_suggestion(start: float, end: float):
    from app.services.clip_analysis import ClipSuggestion

    return ClipSuggestion(
        start_time=start,
        end_time=end,
        duration=end - start,
        virality_score=0.8,
        reason="hook",
        transcript=f"segment from {start} to {end}",
        hooks_matched=["hook"],
    )


class ClipCountAndAspectFanoutTests(_SqliteDbTestCase):
    def _make_job(self, *, source_meta=None, aspect_ratio="9:16") -> RepurposeJob:
        job = RepurposeJob(
            user_id=self.user.id,
            project_id=self.project.id,
            video_id=self.video.id,
            source_mode="project_video",
            clip_mode="basic",
            clip_length_bucket="lt_30",
            aspect_ratio=aspect_ratio,
            source_meta=source_meta,
            status="processing",
        )
        self.db.add(job)
        self.db.flush()
        vt = VideoTranscription(
            video_id=self.video.id,
            status="completed",
            segments=[{"start": 0.0, "end": 5.0, "text": "hello"}],
        )
        self.db.add(vt)
        self.db.commit()
        return job

    def _run_pipeline(self, job: RepurposeJob, suggestions):
        from app.services.repurpose_pipeline import create_clips_for_repurpose_job

        with mock.patch(
            "app.services.repurpose_pipeline.suggest_clips", return_value=suggestions
        ) as suggest_mock, mock.patch(
            "app.jobs.queue.enqueue_clip_render_job", return_value=None
        ), mock.patch(
            "app.services.clip_renderer.fast_thumbnail_for_clip", return_value=None
        ):
            created_ids = create_clips_for_repurpose_job(self.db, job.id)
        return created_ids, suggest_mock

    def test_default_clip_count_is_eight_when_omitted(self):
        job = self._make_job(source_meta=None)
        _, suggest_mock = self._run_pipeline(job, [_fake_suggestion(0, 5)])
        self.assertEqual(suggest_mock.call_args.kwargs["max_suggestions"], 8)

    def test_custom_clip_count_is_respected(self):
        job = self._make_job(source_meta={"clip_count": 3})
        _, suggest_mock = self._run_pipeline(job, [_fake_suggestion(0, 5)])
        self.assertEqual(suggest_mock.call_args.kwargs["max_suggestions"], 3)

    def test_backcompat_single_aspect_ratio_no_fanout(self):
        job = self._make_job(source_meta=None, aspect_ratio="16:9")
        created_ids, _ = self._run_pipeline(job, [_fake_suggestion(0, 5), _fake_suggestion(10, 15)])

        self.assertEqual(len(created_ids), 2)
        clips = self.db.query(Clip).filter(Clip.id.in_(created_ids)).all()
        self.assertTrue(all(c.aspect_ratio == "16:9" for c in clips))

    def test_aspect_ratios_fanout_creates_moments_times_aspects(self):
        job = self._make_job(source_meta={"aspect_ratios": ["9:16", "1:1"]})
        suggestions = [_fake_suggestion(0, 5), _fake_suggestion(10, 15)]
        created_ids, _ = self._run_pipeline(job, suggestions)

        # 2 moments * 2 aspect ratios = 4 clips.
        self.assertEqual(len(created_ids), 4)
        clips = self.db.query(Clip).filter(Clip.id.in_(created_ids)).all()
        by_ratio = {}
        for c in clips:
            by_ratio.setdefault(c.aspect_ratio, set()).add((c.start_time, c.end_time))
        self.assertEqual(set(by_ratio.keys()), {"9:16", "1:1"})
        self.assertEqual(by_ratio["9:16"], {(0.0, 5.0), (10.0, 15.0)})
        self.assertEqual(by_ratio["1:1"], {(0.0, 5.0), (10.0, 15.0)})

    def test_job_created_clip_ids_reflects_total_fanned_out_clips(self):
        job = self._make_job(source_meta={"aspect_ratios": ["9:16", "1:1", "16:9"], "clip_count": 2})
        suggestions = [_fake_suggestion(0, 5), _fake_suggestion(10, 15)]
        created_ids, suggest_mock = self._run_pipeline(job, suggestions)

        self.assertEqual(suggest_mock.call_args.kwargs["max_suggestions"], 2)
        self.assertEqual(len(created_ids), 6)  # 2 moments * 3 aspect ratios
        self.db.refresh(job)
        self.assertEqual(sorted(job.created_clip_ids), sorted(created_ids))
        self.assertEqual(job.status, "completed")


# --- 3. create_youtube_source_video shared service --------------------------


class CreateYoutubeSourceVideoTests(_SqliteDbTestCase):
    def test_resolves_stream_and_metadata_and_enqueues(self):
        from app.services.youtube_source_video import create_youtube_source_video

        with mock.patch(
            "app.services.youtube_source_video.resolve_youtube_page_to_stream_url",
            return_value="https://cdn.test/stream.m3u8",
        ), mock.patch(
            "app.services.youtube_source_video.fetch_youtube_page_metadata",
            return_value={"title": "Cool Video", "thumbnail": "https://img.test/t.jpg", "duration": 125.4},
        ), mock.patch(
            "app.jobs.queue.enqueue_transcription_job", return_value=True
        ) as enqueue_mock:
            video = create_youtube_source_video(
                self.db,
                user=self.user,
                project_id=self.project.id,
                youtube_url="https://www.youtube.com/watch?v=abc123XYZ",
                language="JA",
                enqueue_transcription=True,
            )

        self.assertEqual(video.name, "Cool Video")
        self.assertEqual(video.file_path, "https://cdn.test/stream.m3u8")
        self.assertEqual(video.ingest_page_url, "https://www.youtube.com/watch?v=abc123XYZ")
        self.assertEqual(video.thumbnail_url, "https://img.test/t.jpg")
        self.assertEqual(video.duration, 125)

        vt = (
            self.db.query(VideoTranscription)
            .filter(VideoTranscription.video_id == video.id)
            .first()
        )
        self.assertEqual(vt.language, "ja")
        self.assertEqual(vt.status, "queued")
        enqueue_mock.assert_called_once_with(video.id, language="ja")

    def test_explicit_metadata_skips_yt_dlp_metadata_fetch(self):
        from app.services.youtube_source_video import create_youtube_source_video

        with mock.patch(
            "app.services.youtube_source_video.resolve_youtube_page_to_stream_url",
            return_value="https://cdn.test/stream.m3u8",
        ), mock.patch(
            "app.services.youtube_source_video.fetch_youtube_page_metadata"
        ) as metadata_mock, mock.patch(
            "app.jobs.queue.enqueue_transcription_job", return_value=True
        ):
            video = create_youtube_source_video(
                self.db,
                user=self.user,
                project_id=self.project.id,
                youtube_url="https://www.youtube.com/watch?v=abc123XYZ",
                name="Caller-provided title",
                thumbnail_url="https://img.test/caller.jpg",
                duration_seconds=42,
                enqueue_transcription=False,
            )

        metadata_mock.assert_not_called()
        self.assertEqual(video.name, "Caller-provided title")
        self.assertEqual(video.thumbnail_url, "https://img.test/caller.jpg")
        self.assertEqual(video.duration, 42)

    def test_resolve_failure_raises_stream_resolve_error(self):
        from app.services.youtube_source_video import create_youtube_source_video

        with mock.patch(
            "app.services.youtube_source_video.resolve_youtube_page_to_stream_url",
            side_effect=YoutubeStreamResolveError("no stream found"),
        ):
            with self.assertRaises(YoutubeStreamResolveError):
                create_youtube_source_video(
                    self.db,
                    user=self.user,
                    project_id=self.project.id,
                    youtube_url="https://www.youtube.com/watch?v=doesnotexist",
                )

    def test_enqueue_false_seeds_pending_row_without_enqueueing(self):
        from app.services.youtube_source_video import create_youtube_source_video

        with mock.patch(
            "app.services.youtube_source_video.resolve_youtube_page_to_stream_url",
            return_value="https://cdn.test/stream.m3u8",
        ), mock.patch(
            "app.services.youtube_source_video.fetch_youtube_page_metadata",
            return_value={},
        ), mock.patch(
            "app.jobs.queue.enqueue_transcription_job"
        ) as enqueue_mock:
            video = create_youtube_source_video(
                self.db,
                user=self.user,
                project_id=self.project.id,
                youtube_url="https://www.youtube.com/watch?v=abc123XYZ",
                enqueue_transcription=False,
            )

        enqueue_mock.assert_not_called()
        vt = (
            self.db.query(VideoTranscription)
            .filter(VideoTranscription.video_id == video.id)
            .first()
        )
        self.assertEqual(vt.status, "pending")


# --- 4. POST /projects/{id}/videos/youtube ----------------------------------


class CreateVideoFromYoutubeRouteTests(_SqliteDbTestCase):
    def test_happy_path_creates_video(self):
        from app.api.routes import videos as videos_routes
        from app.api.models.videos import YoutubeVideoCreate

        with mock.patch.object(
            videos_routes, "can_access_project", return_value=True
        ), mock.patch.object(
            videos_routes, "assert_write_project_content", return_value=None
        ), mock.patch.object(
            videos_routes, "log_activity", return_value=None
        ), mock.patch.object(
            videos_routes, "_video_detail", return_value={"ok": True}
        ), mock.patch(
            "app.services.youtube_source_video.resolve_youtube_page_to_stream_url",
            return_value="https://cdn.test/stream.m3u8",
        ), mock.patch(
            "app.services.youtube_source_video.fetch_youtube_page_metadata",
            return_value={"title": "A Talk", "thumbnail": "https://img.test/a.jpg", "duration": 600},
        ), mock.patch(
            "app.jobs.queue.enqueue_transcription_job", return_value=True
        ) as enqueue_mock:
            result = videos_routes.create_video_from_youtube(
                project_id=self.project.id,
                body=YoutubeVideoCreate(url="https://www.youtube.com/watch?v=xyz987ABC", language="en"),
                db=self.db,
                current_user=self.user,
            )

        self.assertEqual(result, {"ok": True})
        created = (
            self.db.query(Video)
            .filter(Video.project_id == self.project.id, Video.name == "A Talk")
            .first()
        )
        self.assertIsNotNone(created)
        self.assertEqual(created.ingest_page_url, "https://www.youtube.com/watch?v=xyz987ABC")
        vt = (
            self.db.query(VideoTranscription)
            .filter(VideoTranscription.video_id == created.id)
            .first()
        )
        self.assertEqual(vt.language, "en")
        enqueue_mock.assert_called_once_with(created.id, language="en")

    def test_resolve_failure_returns_422(self):
        from app.api.routes import videos as videos_routes
        from app.api.models.videos import YoutubeVideoCreate

        with mock.patch.object(
            videos_routes, "can_access_project", return_value=True
        ), mock.patch.object(
            videos_routes, "assert_write_project_content", return_value=None
        ), mock.patch(
            "app.services.youtube_source_video.resolve_youtube_page_to_stream_url",
            side_effect=YoutubeStreamResolveError("yt-dlp could not resolve a stream"),
        ):
            with self.assertRaises(HTTPException) as ctx:
                videos_routes.create_video_from_youtube(
                    project_id=self.project.id,
                    body=YoutubeVideoCreate(url="https://www.youtube.com/watch?v=broken000"),
                    db=self.db,
                    current_user=self.user,
                )

        self.assertEqual(ctx.exception.status_code, 422)


# --- 5. POST /projects/{id}/videos/from-upload ------------------------------


class RegisterUploadedVideoRouteTests(_SqliteDbTestCase):
    def test_creates_video_and_pending_transcription_with_language(self):
        from app.api.routes import videos as videos_routes
        from app.api.models.videos import VideoFromUploadCreate

        with mock.patch.object(
            videos_routes, "can_access_project", return_value=True
        ), mock.patch.object(
            videos_routes, "assert_write_project_content", return_value=None
        ), mock.patch.object(
            videos_routes, "assert_storage_upload_allowed", return_value=None
        ) as quota_mock, mock.patch.object(
            videos_routes, "log_activity", return_value=None
        ), mock.patch.object(
            videos_routes, "_video_detail", return_value={"ok": True}
        ), mock.patch(
            "app.jobs.queue.enqueue_transcription_job", return_value=True
        ) as enqueue_mock, mock.patch(
            "app.jobs.queue.enqueue_video_thumbnail_job", return_value=True
        ), mock.patch(
            "app.services.proxy_service.auto_proxy_on_upload", return_value=None
        ):
            result = videos_routes.register_uploaded_video(
                project_id=self.project.id,
                body=VideoFromUploadCreate(
                    file_path="https://cdn.test/already-uploaded.mp4",
                    name="Wizard Upload",
                    language="FR",
                    size_bytes=123456,
                ),
                db=self.db,
                current_user=self.user,
            )

        self.assertEqual(result, {"ok": True})
        created = (
            self.db.query(Video)
            .filter(Video.project_id == self.project.id, Video.name == "Wizard Upload")
            .first()
        )
        self.assertIsNotNone(created)
        self.assertEqual(created.file_path, "https://cdn.test/already-uploaded.mp4")
        self.assertEqual(created.size_bytes, 123456)
        self.assertEqual(created.version, 1)

        vt = (
            self.db.query(VideoTranscription)
            .filter(VideoTranscription.video_id == created.id)
            .first()
        )
        self.assertEqual(vt.language, "fr")
        self.assertEqual(vt.status, "queued")
        enqueue_mock.assert_called_once_with(created.id, language="fr")

        # Quota accounting uses size_bytes when given.
        quota_mock.assert_called_once()
        self.assertEqual(quota_mock.call_args.kwargs["incoming_bytes"], 123456)

    def test_missing_file_path_raises_400(self):
        from app.api.routes import videos as videos_routes
        from app.api.models.videos import VideoFromUploadCreate

        with mock.patch.object(
            videos_routes, "can_access_project", return_value=True
        ), mock.patch.object(
            videos_routes, "assert_write_project_content", return_value=None
        ):
            with self.assertRaises(HTTPException) as ctx:
                videos_routes.register_uploaded_video(
                    project_id=self.project.id,
                    body=VideoFromUploadCreate(file_path="   ", name="Empty"),
                    db=self.db,
                    current_user=self.user,
                )

        self.assertEqual(ctx.exception.status_code, 400)

    def test_missing_size_bytes_defaults_quota_check_to_zero(self):
        from app.api.routes import videos as videos_routes
        from app.api.models.videos import VideoFromUploadCreate

        with mock.patch.object(
            videos_routes, "can_access_project", return_value=True
        ), mock.patch.object(
            videos_routes, "assert_write_project_content", return_value=None
        ), mock.patch.object(
            videos_routes, "assert_storage_upload_allowed", return_value=None
        ) as quota_mock, mock.patch.object(
            videos_routes, "log_activity", return_value=None
        ), mock.patch.object(
            videos_routes, "_video_detail", return_value={"ok": True}
        ), mock.patch(
            "app.jobs.queue.enqueue_transcription_job", return_value=True
        ), mock.patch(
            "app.jobs.queue.enqueue_video_thumbnail_job", return_value=True
        ), mock.patch(
            "app.services.proxy_service.auto_proxy_on_upload", return_value=None
        ):
            videos_routes.register_uploaded_video(
                project_id=self.project.id,
                body=VideoFromUploadCreate(
                    file_path="https://cdn.test/no-size.mp4",
                    name="No Size Given",
                ),
                db=self.db,
                current_user=self.user,
            )

        self.assertEqual(quota_mock.call_args.kwargs["incoming_bytes"], 0)


# --- 6. POST /repurpose/jobs threads `language` into seeded source videos --


class CreateRepurposeJobLanguageTests(_SqliteDbTestCase):
    """Task: final-review finding #1 — the wizard's repurpose-job endpoint used to
    drop the wizard's spoken-language selection entirely: RepurposeJobCreate had
    no `language` field, so create_repurpose_job never passed one to
    create_youtube_source_video / _create_source_video, and the seeded
    VideoTranscription.language was always NULL regardless of what the user
    picked in the wizard."""

    def test_youtube_source_mode_threads_language_into_seeded_transcription(self):
        from app.api.models.clips import RepurposeJobCreate
        from app.api.routes import clips as clips_routes

        with mock.patch.object(
            clips_routes, "can_access_project", return_value=True
        ), mock.patch.object(
            clips_routes, "assert_write_project_content", return_value=None
        ), mock.patch.object(
            clips_routes, "log_activity", return_value=None
        ), mock.patch.object(
            clips_routes, "start_repurpose_processing", return_value=None
        ), mock.patch(
            "app.services.youtube_source_video.resolve_youtube_page_to_stream_url",
            return_value="https://cdn.test/stream.m3u8",
        ), mock.patch(
            "app.services.youtube_source_video.fetch_youtube_page_metadata",
            return_value={"title": "Repurpose Source", "thumbnail": None, "duration": 90},
        ):
            body = RepurposeJobCreate(
                source_mode="youtube_url",
                project_id=self.project.id,
                youtube_url="https://www.youtube.com/watch?v=lang0001",
                language="ES",
            )
            result = clips_routes.create_repurpose_job(
                body=body, db=self.db, current_user=self.user
            )

        self.assertIsNotNone(result.video_id)
        created = self.db.query(Video).filter(Video.id == result.video_id).first()
        self.assertIsNotNone(created)
        vt = (
            self.db.query(VideoTranscription)
            .filter(VideoTranscription.video_id == created.id)
            .first()
        )
        self.assertEqual(vt.language, "es")

    def test_upload_source_mode_threads_language_into_seeded_transcription(self):
        from app.api.models.clips import RepurposeJobCreate
        from app.api.routes import clips as clips_routes

        with mock.patch.object(
            clips_routes, "can_access_project", return_value=True
        ), mock.patch.object(
            clips_routes, "assert_write_project_content", return_value=None
        ), mock.patch.object(
            clips_routes, "log_activity", return_value=None
        ), mock.patch.object(
            clips_routes, "start_repurpose_processing", return_value=None
        ):
            body = RepurposeJobCreate(
                source_mode="upload",
                project_id=self.project.id,
                source_file_url="https://cdn.test/uploaded-source.mp4",
                source_title="Uploaded Repurpose Source",
                language="PT",
            )
            result = clips_routes.create_repurpose_job(
                body=body, db=self.db, current_user=self.user
            )

        created = (
            self.db.query(Video)
            .filter(
                Video.project_id == self.project.id,
                Video.name == "Uploaded Repurpose Source",
            )
            .first()
        )
        self.assertIsNotNone(created)
        vt = (
            self.db.query(VideoTranscription)
            .filter(VideoTranscription.video_id == created.id)
            .first()
        )
        self.assertEqual(vt.language, "pt")
        self.assertEqual(result.video_id, created.id)

    def test_language_omitted_seeds_none(self):
        from app.api.models.clips import RepurposeJobCreate
        from app.api.routes import clips as clips_routes

        with mock.patch.object(
            clips_routes, "can_access_project", return_value=True
        ), mock.patch.object(
            clips_routes, "assert_write_project_content", return_value=None
        ), mock.patch.object(
            clips_routes, "log_activity", return_value=None
        ), mock.patch.object(
            clips_routes, "start_repurpose_processing", return_value=None
        ):
            body = RepurposeJobCreate(
                source_mode="upload",
                project_id=self.project.id,
                source_file_url="https://cdn.test/no-lang.mp4",
                source_title="No Lang Source",
            )
            clips_routes.create_repurpose_job(body=body, db=self.db, current_user=self.user)

        created = (
            self.db.query(Video)
            .filter(Video.project_id == self.project.id, Video.name == "No Lang Source")
            .first()
        )
        vt = (
            self.db.query(VideoTranscription)
            .filter(VideoTranscription.video_id == created.id)
            .first()
        )
        self.assertIsNone(vt.language)


class DeferredRepurposeJobTests(_SqliteDbTestCase):
    def test_deferred_job_stays_draft_until_explicit_start(self):
        from app.api.models.clips import RepurposeJobCreate
        from app.api.routes import clips as clips_routes

        with mock.patch.object(
            clips_routes, "can_access_project", return_value=True
        ), mock.patch.object(
            clips_routes, "assert_write_project_content", return_value=None
        ), mock.patch.object(
            clips_routes, "log_activity", return_value=None
        ), mock.patch.object(
            clips_routes, "start_repurpose_processing", return_value=None
        ) as start_mock:
            created = clips_routes.create_repurpose_job(
                body=RepurposeJobCreate(
                    source_mode="project_video",
                    project_id=self.project.id,
                    video_id=self.video.id,
                    auto_start=False,
                ),
                db=self.db,
                current_user=self.user,
            )

            self.assertEqual(created.status, "draft")
            start_mock.assert_not_called()

            started = clips_routes.start_deferred_repurpose_job(
                job_id=created.id, db=self.db, current_user=self.user
            )
            self.assertEqual(started.status, "processing")
            start_mock.assert_called_once_with(self.db, created.id)

            clips_routes.start_deferred_repurpose_job(
                job_id=created.id, db=self.db, current_user=self.user
            )
            start_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
