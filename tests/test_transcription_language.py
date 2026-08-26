"""Task B1: transcription language select/pass-through/persist.

Covers:
- ISO 639-1 normalization ("auto"/""/None -> None).
- prepare_and_enqueue_transcription persists language on the row and threads it
  to the enqueue helper.
- The upload and retry route handlers accept/normalize/pass through `language`
  (enqueue mocked).
- The worker (transcribe_video) passes language= to model.transcribe and
  persists info.language into detected_language, without ever importing a
  real whisper model.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import (
    ActivityFeed,
    Comment,
    Annotation,
    Folder,
    Project,
    User,
    Video,
    VideoApproval,
    VideoTranscription,
)
from app.utils.language import normalize_language


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(type_, compiler, **kw):
    # Tests run against in-memory SQLite; JSONB columns degrade to plain JSON.
    return "JSON"


class NormalizeLanguageTests(unittest.TestCase):
    def test_none_is_auto(self):
        self.assertIsNone(normalize_language(None))

    def test_empty_string_is_auto(self):
        self.assertIsNone(normalize_language(""))

    def test_whitespace_only_is_auto(self):
        self.assertIsNone(normalize_language("   "))

    def test_auto_literal_is_auto(self):
        self.assertIsNone(normalize_language("auto"))
        self.assertIsNone(normalize_language("Auto"))
        self.assertIsNone(normalize_language(" AUTO "))

    def test_language_code_is_stored_lowercased(self):
        self.assertEqual(normalize_language("ja"), "ja")
        self.assertEqual(normalize_language("EN"), "en")
        self.assertEqual(normalize_language(" fr "), "fr")


class _SqliteDbTestCase(unittest.TestCase):
    """Shared in-memory sqlite setup for route/service-level tests."""

    tables = [
        User.__table__,
        Project.__table__,
        Folder.__table__,
        Video.__table__,
        VideoTranscription.__table__,
        Comment.__table__,
        Annotation.__table__,
        # video_detail_dict now reports the latest review decision, so the
        # payload reads this table. (The shared `db_session` fixture in
        # conftest.py builds the whole public schema and avoids this class of
        # breakage — prefer it for new tests.)
        VideoApproval.__table__,
        # Uploading a new version resets the cut to in_review, and that status
        # change is logged.
        ActivityFeed.__table__,
    ]

    def setUp(self) -> None:
        engine = create_engine("sqlite://")
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
            name="Clip",
            version=1,
            file_path="https://example.test/clip.mp4",
            uploader_id=self.user.id,
        )
        self.db.add(self.video)
        self.db.flush()

    def tearDown(self) -> None:
        self.db.close()


class PrepareAndEnqueueTranscriptionLanguageTests(_SqliteDbTestCase):
    def test_creates_row_with_normalized_language(self):
        from app.services.transcription_enqueue import prepare_and_enqueue_transcription

        with mock.patch(
            "app.jobs.queue.enqueue_transcription_job", return_value=True
        ) as enqueue_mock:
            prepare_and_enqueue_transcription(self.db, self.video.id, language="JA")

        vt = (
            self.db.query(VideoTranscription)
            .filter(VideoTranscription.video_id == self.video.id)
            .first()
        )
        self.assertEqual(vt.language, "ja")
        enqueue_mock.assert_called_once_with(self.video.id, language="ja")

    def test_auto_and_empty_normalize_to_none(self):
        from app.services.transcription_enqueue import prepare_and_enqueue_transcription

        for raw in ("auto", "", None):
            with mock.patch("app.jobs.queue.enqueue_transcription_job", return_value=True):
                # None here means "don't touch"; use a fresh row per case except None.
                prepare_and_enqueue_transcription(self.db, self.video.id, language="fr", force=True)
                prepare_and_enqueue_transcription(self.db, self.video.id, language=raw, force=True)
            vt = (
                self.db.query(VideoTranscription)
                .filter(VideoTranscription.video_id == self.video.id)
                .first()
            )
            if raw is None:
                # Omitted => preserve whatever was set right before (still "fr").
                self.assertEqual(vt.language, "fr")
            else:
                self.assertIsNone(vt.language)

    def test_omitting_language_preserves_existing_selection(self):
        from app.services.transcription_enqueue import prepare_and_enqueue_transcription

        with mock.patch("app.jobs.queue.enqueue_transcription_job", return_value=True):
            prepare_and_enqueue_transcription(self.db, self.video.id, language="es")

        with mock.patch(
            "app.jobs.queue.enqueue_transcription_job", return_value=True
        ) as enqueue_mock:
            # Retry without a language param (e.g. force=True) must not reset it.
            prepare_and_enqueue_transcription(self.db, self.video.id, force=True)

        vt = (
            self.db.query(VideoTranscription)
            .filter(VideoTranscription.video_id == self.video.id)
            .first()
        )
        self.assertEqual(vt.language, "es")
        enqueue_mock.assert_called_once_with(self.video.id, language="es")

    def test_no_language_at_all_keeps_auto_default(self):
        from app.services.transcription_enqueue import prepare_and_enqueue_transcription

        with mock.patch(
            "app.jobs.queue.enqueue_transcription_job", return_value=True
        ) as enqueue_mock:
            prepare_and_enqueue_transcription(self.db, self.video.id)

        vt = (
            self.db.query(VideoTranscription)
            .filter(VideoTranscription.video_id == self.video.id)
            .first()
        )
        self.assertIsNone(vt.language)
        enqueue_mock.assert_called_once_with(self.video.id, language=None)


class RetryRouteLanguageTests(_SqliteDbTestCase):
    """POST /videos/{id}/transcription and the project-scoped duplicate."""

    def test_start_video_transcription_passes_language_through(self):
        from app.api.routes import video_detail

        with mock.patch.object(video_detail, "can_access_project", return_value=True), mock.patch.object(
            video_detail, "assert_write_project_content", return_value=None
        ), mock.patch.object(
            video_detail, "prepare_and_enqueue_transcription"
        ) as prep_mock:
            video_detail.start_video_transcription(
                video_id=self.video.id,
                force=False,
                language="pt",
                db=self.db,
                current_user=self.user,
            )

        prep_mock.assert_called_once_with(self.db, self.video.id, force=False, language="pt")

    def test_start_video_transcription_defaults_language_to_none(self):
        from app.api.routes import video_detail

        with mock.patch.object(video_detail, "can_access_project", return_value=True), mock.patch.object(
            video_detail, "assert_write_project_content", return_value=None
        ), mock.patch.object(
            video_detail, "prepare_and_enqueue_transcription"
        ) as prep_mock:
            video_detail.start_video_transcription(
                video_id=self.video.id, force=True, language=None, db=self.db, current_user=self.user
            )

        prep_mock.assert_called_once_with(self.db, self.video.id, force=True, language=None)

    def test_project_scoped_retry_passes_language_through(self):
        from app.api.routes import videos as videos_routes

        with mock.patch.object(videos_routes, "can_access_project", return_value=True), mock.patch.object(
            videos_routes, "assert_write_project_content", return_value=None
        ), mock.patch.object(
            videos_routes, "prepare_and_enqueue_transcription"
        ) as prep_mock:
            videos_routes.start_project_video_transcription(
                project_id=self.project.id,
                video_id=self.video.id,
                force=False,
                language="de",
                db=self.db,
                current_user=self.user,
            )

        prep_mock.assert_called_once_with(self.db, self.video.id, force=False, language="de")


class UploadVideoLanguageTests(_SqliteDbTestCase):
    def test_upload_video_persists_and_enqueues_language(self):
        from app.api.routes import videos as videos_routes

        fake_upload = {"url": "https://cdn.test/video.mp4", "bytes": 1234}

        with mock.patch.object(
            videos_routes, "can_access_project", return_value=True
        ), mock.patch.object(
            videos_routes, "assert_write_project_content", return_value=None
        ), mock.patch.object(
            videos_routes, "assert_storage_upload_allowed", return_value=None
        ), mock.patch.object(
            videos_routes, "upload_file_to_cloudinary_with_meta", return_value=fake_upload
        ), mock.patch.object(
            videos_routes, "log_activity", return_value=None
        ), mock.patch(
            "app.jobs.queue.enqueue_transcription_job", return_value=True
        ) as enqueue_mock, mock.patch(
            "app.jobs.queue.enqueue_video_thumbnail_job", return_value=True
        ), mock.patch(
            "app.services.proxy_service.auto_proxy_on_upload", return_value=None
        ), mock.patch.object(
            videos_routes, "_video_detail", return_value={"ok": True}
        ):
            fake_video_file = mock.MagicMock()
            videos_routes.upload_video(
                project_id=self.project.id,
                video_file=fake_video_file,
                name="Hero Cut",
                description=None,
                folder_id=None,
                version_of=None,
                version_notes=None,
                language="JA",
                db=self.db,
                current_user=self.user,
            )

        created = (
            self.db.query(Video)
            .filter(Video.project_id == self.project.id, Video.name == "Hero Cut")
            .first()
        )
        self.assertIsNotNone(created)
        vt = (
            self.db.query(VideoTranscription)
            .filter(VideoTranscription.video_id == created.id)
            .first()
        )
        self.assertEqual(vt.language, "ja")
        enqueue_mock.assert_called_once_with(created.id, language="ja")

    def test_upload_video_without_language_stays_auto(self):
        from app.api.routes import videos as videos_routes

        fake_upload = {"url": "https://cdn.test/video2.mp4", "bytes": 999}

        with mock.patch.object(
            videos_routes, "can_access_project", return_value=True
        ), mock.patch.object(
            videos_routes, "assert_write_project_content", return_value=None
        ), mock.patch.object(
            videos_routes, "assert_storage_upload_allowed", return_value=None
        ), mock.patch.object(
            videos_routes, "upload_file_to_cloudinary_with_meta", return_value=fake_upload
        ), mock.patch.object(
            videos_routes, "log_activity", return_value=None
        ), mock.patch(
            "app.jobs.queue.enqueue_transcription_job", return_value=True
        ) as enqueue_mock, mock.patch(
            "app.jobs.queue.enqueue_video_thumbnail_job", return_value=True
        ), mock.patch(
            "app.services.proxy_service.auto_proxy_on_upload", return_value=None
        ), mock.patch.object(
            videos_routes, "_video_detail", return_value={"ok": True}
        ):
            fake_video_file = mock.MagicMock()
            videos_routes.upload_video(
                project_id=self.project.id,
                video_file=fake_video_file,
                name="No Lang Cut",
                description=None,
                folder_id=None,
                version_of=None,
                version_notes=None,
                language=None,
                db=self.db,
                current_user=self.user,
            )

        created = (
            self.db.query(Video)
            .filter(Video.project_id == self.project.id, Video.name == "No Lang Cut")
            .first()
        )
        vt = (
            self.db.query(VideoTranscription)
            .filter(VideoTranscription.video_id == created.id)
            .first()
        )
        self.assertIsNone(vt.language)
        enqueue_mock.assert_called_once_with(created.id, language=None)


class UploadVideoVersionOfTests(_SqliteDbTestCase):
    """Route-level: POST /projects/{project_id}/videos with a real `version_of`
    id goes through the refactored `resolve_version_chain` call in
    `upload_video` end-to-end (D6 follow-up #1)."""

    def test_upload_video_with_version_of_inherits_group_backfills_base_and_folder(self):
        from app.api.routes import videos as videos_routes

        folder = Folder(project_id=self.project.id, name="Main Cuts", created_by=self.user.id)
        self.db.add(folder)
        self.db.flush()

        # Base video predates version_group_id (like a legacy row) and lives
        # in a folder — the new version must inherit both.
        self.video.folder_id = folder.id
        self.db.commit()
        self.assertIsNone(self.video.version_group_id)

        fake_upload = {"url": "https://cdn.test/video-v2.mp4", "bytes": 5555}

        with mock.patch.object(
            videos_routes, "can_access_project", return_value=True
        ), mock.patch.object(
            videos_routes, "assert_write_project_content", return_value=None
        ), mock.patch.object(
            videos_routes, "assert_storage_upload_allowed", return_value=None
        ), mock.patch.object(
            videos_routes, "upload_file_to_cloudinary_with_meta", return_value=fake_upload
        ), mock.patch.object(
            videos_routes, "log_activity", return_value=None
        ), mock.patch(
            "app.jobs.queue.enqueue_transcription_job", return_value=True
        ), mock.patch(
            "app.jobs.queue.enqueue_video_thumbnail_job", return_value=True
        ), mock.patch(
            "app.services.proxy_service.auto_proxy_on_upload", return_value=None
        ):
            fake_video_file = mock.MagicMock()
            result = videos_routes.upload_video(
                project_id=self.project.id,
                video_file=fake_video_file,
                name="Hero Cut v2",
                description=None,
                folder_id=None,
                version_of=self.video.id,
                version_notes=None,
                language=None,
                db=self.db,
                current_user=self.user,
            )

        # Response carries the resolved version and inherited folder.
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["folder_id"], folder.id)

        new_video = (
            self.db.query(Video)
            .filter(Video.project_id == self.project.id, Video.name == "Hero Cut v2")
            .first()
        )
        self.assertIsNotNone(new_video)
        self.assertEqual(new_video.version, 2)
        self.assertEqual(new_video.folder_id, folder.id)

        # Base video's version_group_id was backfilled in place, and the new
        # version inherited that same (backfilled) group id.
        self.db.refresh(self.video)
        self.assertIsNotNone(self.video.version_group_id)
        self.assertEqual(new_video.version_group_id, self.video.version_group_id)


class _FakeQuery:
    def __init__(self, result):
        self._result = result

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def first(self):
        return self._result


class TranscribeVideoWorkerLanguageTests(unittest.TestCase):
    """Worker: language= threaded into model.transcribe(); info.language persisted."""

    def _make_db(self, vt, video, job=None):
        from app.db.models import RepurposeJob, Video as VideoModel, VideoTranscription as VTModel

        db = mock.MagicMock()

        def query_side_effect(model, *a, **k):
            if model is VTModel:
                return _FakeQuery(vt)
            if model is VideoModel:
                return _FakeQuery(video)
            if model is RepurposeJob:
                return _FakeQuery(job)
            return _FakeQuery(None)

        db.query.side_effect = query_side_effect
        return db

    def _make_fake_whisper_model(self, detected_language: str = "en"):
        segment = SimpleNamespace(start=0.0, end=1.5, text="hello world")
        info = SimpleNamespace(language=detected_language)
        fake_instance = mock.MagicMock()
        fake_instance.transcribe.return_value = ([segment], info)
        fake_model_cls = mock.MagicMock(return_value=fake_instance)
        return fake_model_cls, fake_instance

    def _run_transcribe_video(self, *, vt_language=None, job_language=None, detected="en"):
        from app.jobs import transcription as job_mod

        vt = SimpleNamespace(
            status="pending",
            error_message=None,
            language=vt_language,
            segments=None,
            speakers=None,
            speaker_count=None,
            model_name=None,
            detected_language=None,
        )
        video = SimpleNamespace(
            id=1,
            duration=10,
            file_path="https://example.test/audio-source.mp4",
            ingest_page_url=None,
        )
        db = self._make_db(vt, video)
        fake_model_cls, fake_instance = self._make_fake_whisper_model(detected_language=detected)

        def fake_ffmpeg(input_src, wav_path, **kwargs):
            wav_path.write_bytes(b"\x00" * 20000)

        with mock.patch.object(job_mod, "SessionLocal", return_value=db), mock.patch.object(
            job_mod, "_run_ffmpeg_to_wav", side_effect=fake_ffmpeg
        ), mock.patch("faster_whisper.WhisperModel", fake_model_cls), mock.patch(
            "app.services.repurpose_pipeline.create_clips_for_completed_repurpose_jobs",
            return_value=None,
        ):
            job_mod.transcribe_video(video.id, job_language)

        return vt, fake_instance

    def test_explicit_job_arg_language_passed_to_whisper(self):
        vt, fake_instance = self._run_transcribe_video(vt_language=None, job_language="ES", detected="es")
        _, kwargs = fake_instance.transcribe.call_args
        self.assertEqual(kwargs.get("language"), "es")
        self.assertEqual(vt.detected_language, "es")
        self.assertEqual(vt.status, "completed")

    def test_row_language_used_when_job_arg_absent(self):
        vt, fake_instance = self._run_transcribe_video(vt_language="ja", job_language=None, detected="ja")
        _, kwargs = fake_instance.transcribe.call_args
        self.assertEqual(kwargs.get("language"), "ja")
        self.assertEqual(vt.detected_language, "ja")

    def test_auto_mode_passes_none_and_still_persists_detected_language(self):
        vt, fake_instance = self._run_transcribe_video(vt_language=None, job_language=None, detected="fr")
        _, kwargs = fake_instance.transcribe.call_args
        self.assertIsNone(kwargs.get("language"))
        self.assertEqual(vt.detected_language, "fr")

    def test_job_arg_auto_falls_back_to_row_language(self):
        vt, fake_instance = self._run_transcribe_video(vt_language="de", job_language="auto", detected="de")
        _, kwargs = fake_instance.transcribe.call_args
        self.assertEqual(kwargs.get("language"), "de")
        self.assertEqual(vt.detected_language, "de")


if __name__ == "__main__":
    unittest.main()
