"""
RQ job: download video audio, run faster-whisper, persist segments to video_transcriptions.
Run worker from repo editube/ directory:
  rq worker -u "$REDIS_URL" default
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

from sqlalchemy import update
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from app.db.database import SessionLocal
from app.db.models import Project, RepurposeJob, UserSettings, Video, VideoTranscription
from app.services.activation_analytics import record_first_value
from app.services.product_analytics import emit
from app.services.transcription_models import resolve_runtime
from app.utils.language import normalize_language

logger = logging.getLogger(__name__)


def _ensure_worker_logging() -> None:
    """RQ workers do not import app.main; ensure INFO logs reach stderr."""
    root = logging.getLogger()
    if root.handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )


# PCM16 mono 16kHz: below ~1s of silence is still > ~32kB; tiny files imply no usable audio.
_MIN_WAV_BYTES_FOR_WHISPER = 8000


class TranscriptionTerminalFailure(RuntimeError):
    """Failure already recorded on the transcription row."""


def _touch_transcription_timestamp(db: Session, video_id: int) -> None:
    """Let GET /videos/{id} show liveness while ffmpeg/Whisper run for a long time."""
    try:
        db.execute(
            update(VideoTranscription)
            .where(VideoTranscription.video_id == video_id)
            .values(updated_at=func.now())
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Could not touch transcription updated_at for video %s", video_id)


def _ffmpeg_stderr_suggests_no_audio(stderr: str) -> bool:
    s = (stderr or "").lower()
    needles = (
        "no audio",
        "does not contain any stream",
        "matches no streams",
        "could not find codec parameters for stream",
        "invalid data found when processing input",
        "audio: none",
        "0 channels",
    )
    return any(n in s for n in needles)


def _run_ffmpeg_to_wav(
    input_src: str,
    wav_path: Path,
    *,
    start_seconds: float = 0.0,
    end_seconds: float | None = None,
) -> None:
    """
    Extract 16 kHz mono PCM WAV from a local path or http(s) URL.
    Prefer this over downloading the full video into RAM — ffmpeg streams the input.
    """
    ua = (os.environ.get("FFMPEG_USER_AGENT") or "").strip()
    cmd: list[str] = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
    ]
    if ua:
        cmd.extend(["-user_agent", ua])
    if start_seconds > 0:
        cmd.extend(["-ss", f"{start_seconds:.3f}"])
    cmd.extend(
        [
            "-i",
            input_src,
        ]
    )
    if end_seconds is not None and end_seconds > start_seconds:
        cmd.extend(["-t", f"{end_seconds - start_seconds:.3f}"])
    cmd.extend(
        [
            "-vn",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(wav_path),
        ]
    )
    timeout_sec = int(os.environ.get("FFMPEG_PROCESS_TIMEOUT_SEC", "10800") or "10800")
    timeout_sec = max(120, min(timeout_sec, 86400))
    subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
    )


def _job_source_range(job: RepurposeJob | None) -> tuple[float, float | None]:
    if not job:
        return 0.0, None
    meta = job.source_meta if isinstance(job.source_meta, dict) else {}
    try:
        start = float(meta.get("source_range_start_seconds") or 0)
    except (TypeError, ValueError):
        start = 0.0
    end_raw = meta.get("source_range_end_seconds")
    if end_raw is None:
        end_raw = job.source_trim_seconds
    try:
        end = float(end_raw) if end_raw is not None else None
    except (TypeError, ValueError):
        end = None
    if end is not None and end <= start:
        end = None
    return max(0.0, start), end


def _preferred_transcription_model(db: Session, video: Video | None) -> str | None:
    """The uploader's Settings → AI models transcription choice, if any.

    Returns None when they have not chosen one — `resolve_runtime` then falls
    through to `default_transcription_model_id()`, the same function the picker
    reads its default from. Resolving the env var here instead would let the
    worker run one model while the settings panel displayed another.
    """
    user_id = getattr(video, "uploader_id", None)
    if user_id:
        try:
            row = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
            prefs = row.ai_model_preferences if row else None
            if isinstance(prefs, dict):
                chosen = (prefs.get("transcription") or "").strip()
                if chosen:
                    return chosen
        except Exception:
            # A settings lookup must never take the transcription down.
            db.rollback()
            logger.exception("Could not read transcription model preference for video")
    return None


def _preferred_transcription_model_id(db: Session, video_id: int) -> str:
    """Catalog id to persist on a row when the run never got far enough to pick one."""
    video = db.query(Video).filter(Video.id == video_id).first()
    model, _size, _fallback = resolve_runtime(_preferred_transcription_model(db, video))
    return model.id


def _transcription_analytics_context(db: Session, video_id: int):
    video = db.query(Video).filter(Video.id == video_id).first()
    project_id = getattr(video, "project_id", None)
    project = (
        db.query(Project).filter(Project.id == project_id).first()
        if project_id is not None
        else None
    )
    return video, project


def _emit_transcription_failure(db: Session, video_id: int, error_code: str) -> None:
    video, project = _transcription_analytics_context(db, video_id)
    if not video:
        return
    project_id = getattr(video, "project_id", None)
    uploader_id = getattr(video, "uploader_id", None)
    properties = {
        "feature_key": "transcript_edit",
        "project_id": project_id,
        "video_id": video_id,
        "failure_class": "processing",
        "error_code": error_code,
        "result": "failure",
    }
    emit(
        db,
        "transcription_failed",
        user_id=uploader_id,
        workspace_id=project.workspace_id if project else None,
        properties=properties,
        source="worker",
    )
    emit(
        db,
        "feature_failed",
        user_id=uploader_id,
        workspace_id=project.workspace_id if project else None,
        properties=properties,
        source="worker",
    )


def transcribe_video(video_id: int, language: str | None = None) -> None:
    _ensure_worker_logging()
    logger.info("transcribe_video: starting job for video_id=%s", video_id)
    db: Session = SessionLocal()
    try:
        vt = db.query(VideoTranscription).filter(VideoTranscription.video_id == video_id).first()
        video = db.query(Video).filter(Video.id == video_id).first()
        if not video or not vt:
            raise RuntimeError(
                f"Video or transcription row {video_id} was removed before processing"
            )

        # Prefer the explicit job arg, fall back to whatever was persisted on the row.
        requested_language = normalize_language(language) or normalize_language(vt.language)

        vt.status = "processing"
        vt.error_message = None
        project_id = getattr(video, "project_id", None)
        uploader_id = getattr(video, "uploader_id", None)
        project = (
            db.query(Project).filter(Project.id == project_id).first()
            if project_id is not None
            else None
        )
        start_properties = {
            "feature_key": "transcript_edit",
            "project_id": project_id,
            "video_id": video_id,
            "requested_language": requested_language or "auto",
            "result": "started",
        }
        emit(
            db,
            "transcription_started",
            user_id=uploader_id,
            workspace_id=project.workspace_id if project else None,
            properties=start_properties,
            source="worker",
        )
        emit(
            db,
            "feature_started",
            user_id=uploader_id,
            workspace_id=project.workspace_id if project else None,
            properties=start_properties,
            source="worker",
        )
        db.commit()

        # Honour the user's Settings → AI models choice. Previously this read
        # WHISPER_MODEL_SIZE only, so picking a model in the UI changed nothing.
        selected_model, model_size, engine_fallback = resolve_runtime(
            _preferred_transcription_model(db, video)
        )
        if engine_fallback:
            logger.info(
                "transcribe_video: %s runs on engine '%s', which has no adapter here; "
                "using faster-whisper '%s' for video_id=%s",
                selected_model.label,
                selected_model.engine,
                model_size,
                video_id,
            )
        device = os.environ.get("TRANSCRIPTION_DEVICE", "cpu").strip() or "cpu"
        compute_type = os.environ.get("TRANSCRIPTION_COMPUTE_TYPE", "int8").strip() or "int8"

        job = (
            db.query(RepurposeJob)
            .filter(RepurposeJob.video_id == video_id)
            .order_by(RepurposeJob.id.desc())
            .first()
        )
        video_duration = float(video.duration) if video.duration else None
        range_start, range_end = _job_source_range(job)
        if video_duration is None and range_end is not None:
            video_duration = range_end
        media_src = str(video.file_path or "")
        from app.services.youtube_stream_resolve import (
            YoutubeStreamResolveError,
            resolve_youtube_page_to_audio_stream_url,
            resolve_youtube_page_to_stream_url,
        )

        page_for_audio = (video.ingest_page_url or "").strip()
        if not page_for_audio and job and job.source_mode == "youtube_url":
            page_for_audio = (job.source_url or "").strip()

        # YouTube: transcribe from **audio** DASH using the canonical **watch URL** stored on
        # the video row (`ingest_page_url`) or repurpose job — not `file_path` (often video-only).
        if page_for_audio:
            try:
                logger.info(
                    "transcribe_video: resolving YouTube audio for video_id=%s (page URL present)",
                    video_id,
                )
                media_src = resolve_youtube_page_to_audio_stream_url(page_for_audio)
            except YoutubeStreamResolveError as exc:
                logger.warning(
                    "Could not resolve audio stream from page URL for video %s (falling back to file_path): %s",
                    video_id,
                    exc,
                )
                media_src = str(video.file_path or "")
        elif "googlevideo.com" in media_src or (
            "videoplayback" in media_src and "expire=" in media_src
        ):
            if job and job.source_mode == "youtube_url" and (job.source_url or "").strip():
                try:
                    media_src = resolve_youtube_page_to_stream_url(job.source_url.strip())
                    video.file_path = media_src
                    db.add(video)
                    db.commit()
                    db.refresh(video)
                except YoutubeStreamResolveError as exc:
                    logger.warning(
                        "Could not refresh YouTube signed URL for video %s (using stored URL): %s",
                        video_id,
                        exc,
                    )
                    media_src = str(video.file_path or "")

        with tempfile.TemporaryDirectory() as tmp:
            wav_path = Path(tmp) / "audio.wav"
            logger.info(
                "transcribe_video: extracting WAV via ffmpeg for video_id=%s (this can take a while for long sources)",
                video_id,
            )
            _touch_transcription_timestamp(db, video_id)
            _run_ffmpeg_to_wav(
                media_src,
                wav_path,
                start_seconds=range_start,
                end_seconds=range_end,
            )
            _touch_transcription_timestamp(db, video_id)

            wav_size = wav_path.stat().st_size if wav_path.exists() else 0
            if wav_size < _MIN_WAV_BYTES_FOR_WHISPER:
                logger.info(
                    "WAV too small for video %s (%s bytes); likely no usable audio (e.g. video-only URL)",
                    video_id,
                    wav_size,
                )
                vt.segments = []
                vt.speakers = []
                vt.speaker_count = 0
                vt.status = "failed"
                vt.model_name = selected_model.id
                vt.error_message = (
                    "No usable audio extracted (file may be video-only). For YouTube imports, "
                    "re-run transcription with an updated worker that uses a dedicated audio stream."
                )
                _emit_transcription_failure(db, video_id, "no_usable_audio")
                db.commit()
                from app.services.repurpose_pipeline import mark_repurpose_jobs_failed

                mark_repurpose_jobs_failed(db, video_id, vt.error_message or "No usable audio extracted")
                raise TranscriptionTerminalFailure(vt.error_message)

            from faster_whisper import WhisperModel

            logger.info(
                "transcribe_video: running Whisper model=%s device=%s for video_id=%s",
                model_size,
                device,
                video_id,
            )
            _touch_transcription_timestamp(db, video_id)
            model = WhisperModel(model_size, device=device, compute_type=compute_type)
            beam = int(os.environ.get("WHISPER_BEAM_SIZE", "1").strip() or "1")
            beam = max(1, min(beam, 5))

            # Silero VAD (bundled with faster-whisper) filters non-speech before Whisper.
            # This makes segment start/end times accurate speech boundaries so downstream
            # silence detection (gap analysis) works on real pauses, not Whisper artifacts.
            vad_enabled = os.environ.get("WHISPER_VAD_FILTER", "true").strip().lower() not in {"0", "false", "no"}
            vad_params: dict = {}
            if vad_enabled:
                try:
                    vad_params = {
                        "threshold": float(os.environ.get("WHISPER_VAD_THRESHOLD", "0.5") or "0.5"),
                        "min_silence_duration_ms": int(os.environ.get("WHISPER_VAD_MIN_SILENCE_MS", "500") or "500"),
                        "speech_pad_ms": int(os.environ.get("WHISPER_VAD_SPEECH_PAD_MS", "200") or "200"),
                    }
                except (TypeError, ValueError):
                    vad_params = {}

            # Word-level timestamps let downstream analysis (filler/bad-take
            # suggestion timing in app.services.auto_edit) snap to real word
            # boundaries instead of faking them via even division. It adds
            # CPU cost on top of plain segment decoding, so it's gated behind
            # an env flag (default on).
            word_timestamps_enabled = os.environ.get(
                "WHISPER_WORD_TIMESTAMPS", "1"
            ).strip().lower() not in {"0", "false", "no"}

            segments_iter, info = model.transcribe(
                str(wav_path),
                beam_size=beam,
                vad_filter=vad_enabled,
                vad_parameters=vad_params if vad_enabled else None,
                language=requested_language,
                word_timestamps=word_timestamps_enabled,
            )

            # Persist what Whisper actually detected/used, even in auto mode.
            vt.detected_language = getattr(info, "language", None)
            db.commit()

            segments: list[dict] = []
            speaker_labels: set[str] = set()
            seg_count = 0
            for seg in segments_iter:
                # Minimal diarization-compatible shape.
                # We preserve a deterministic speaker tag even before whisperx integration.
                speaker = "SPEAKER_1"
                speaker_labels.add(speaker)
                seg_dict: dict = {
                    "start": float(seg.start) + range_start,
                    "end": float(seg.end) + range_start,
                    "text": (seg.text or "").strip(),
                    "speaker": speaker,
                }

                seg_words = getattr(seg, "words", None)
                if seg_words:
                    words_out: list[dict] = []
                    for w in seg_words:
                        word_text = str(getattr(w, "word", "") or "").strip()
                        if not word_text:
                            continue
                        words_out.append(
                            {
                                "word": word_text,
                                "start": round(float(w.start) + range_start, 3),
                                "end": round(float(w.end) + range_start, 3),
                            }
                        )
                    if words_out:
                        seg_dict["words"] = words_out

                segments.append(seg_dict)
                seg_count += 1
                if seg_count % 50 == 0:
                    try:
                        db.execute(
                            update(VideoTranscription)
                            .where(VideoTranscription.video_id == video_id)
                            .values(updated_at=func.now())
                        )
                        db.commit()
                    except Exception:
                        db.rollback()
                        logger.exception("Transcription progress touch failed for video %s", video_id)

            # Audio-level speech/silence analysis (Silero VAD over the same
            # WAV Whisper just consumed). Segment gaps cannot see pauses that
            # Whisper merged into one segment; this can. Enhancement only —
            # never fails the job.
            audio_analysis = None
            try:
                from app.services.audio_analysis import analyze_wav_speech

                _touch_transcription_timestamp(db, video_id)
                audio_analysis = analyze_wav_speech(wav_path, offset_seconds=range_start)
            except Exception:
                logger.exception("Audio analysis failed for video %s", video_id)

            if audio_analysis:
                # The WAV knows the real duration; the videos row often doesn't
                # (NULL for direct uploads). Only trust it when the WAV covers
                # the whole source, i.e. no source-range trim was applied.
                analysis_duration = float(audio_analysis.get("duration") or 0)
                if video_duration is None and range_start == 0 and range_end is None and analysis_duration > 0:
                    video_duration = analysis_duration
                    if not video.duration:
                        video.duration = int(round(analysis_duration))
                        db.add(video)

            nonempty = [s for s in segments if (s.get("text") or "").strip()]
            if not nonempty:
                vt.segments = []
                vt.speakers = []
                vt.speaker_count = 0
                vt.status = "failed"
                vt.model_name = selected_model.id
                vt.error_message = (
                    "Whisper produced no speech. Common cause: video-only stream with no audio track. "
                    "Use Force new job after updating the worker, or re-import from YouTube."
                )
                _emit_transcription_failure(db, video_id, "no_speech_detected")
                db.commit()
                logger.warning("Transcription produced zero segments for video %s", video_id)
                from app.services.repurpose_pipeline import mark_repurpose_jobs_failed

                mark_repurpose_jobs_failed(db, video_id, vt.error_message or "Whisper produced no speech")
                raise TranscriptionTerminalFailure(vt.error_message)

            vt.segments = nonempty
            vt.speakers = sorted(speaker_labels)
            vt.speaker_count = len(speaker_labels)
            vt.audio_analysis = audio_analysis
            vt.status = "completed"
            vt.model_name = selected_model.id
            vt.error_message = None
            completed_properties = {
                "feature_key": "transcript_edit",
                "project_id": project_id,
                "video_id": video_id,
                "model_id": selected_model.id,
                "detected_language": vt.detected_language or requested_language or "unknown",
                "segment_count": len(nonempty),
                "duration_seconds": round(float(video_duration or 0), 2),
                "result": "success",
            }
            emit(
                db,
                "transcription_completed",
                user_id=uploader_id,
                workspace_id=project.workspace_id if project else None,
                properties=completed_properties,
                source="worker",
            )
            emit(
                db,
                "feature_completed",
                user_id=uploader_id,
                workspace_id=project.workspace_id if project else None,
                properties={**completed_properties, "completion_type": "transcript_ready"},
                source="worker",
            )
            if project and project.workspace_id is not None:
                emit(
                    db,
                    "project_setup_completed",
                    user_id=uploader_id,
                    workspace_id=project.workspace_id,
                    properties={
                        "project_id": project_id,
                        "video_id": video_id,
                        "completion_type": "transcript_ready",
                        "result": "success",
                    },
                    source="worker",
                )
                record_first_value(
                    db,
                    user_id=uploader_id,
                    workspace_id=project.workspace_id,
                    feature_key="transcript_edit",
                    resource_type="transcription",
                    resource_id=vt.id,
                )
            db.commit()
            logger.info("Transcription completed for video %s (%s segments)", video_id, len(nonempty))

            # Realtime hint so open editors refresh immediately instead of
            # waiting out a poll interval. Best-effort by design.
            from app.services.realtime import publish_video_update

            publish_video_update(
                uploader_id, video_id, kind="transcription", status="completed"
            )

            # Server-side auto-edit: if the video has enabled auto-edit prefs,
            # analyze the transcript and seed/merge keepRanges + aiAnalysis into
            # the rough_cut_draft *before* the repurpose (clips) chain below, so
            # clip suggestion windows can read the freshly seeded keepRanges.
            # This must never fail the transcription job itself.
            try:
                from app.services.auto_edit import run_post_transcription_auto_edit

                run_post_transcription_auto_edit(
                    db,
                    video_id,
                    segments=nonempty,
                    video_duration=video_duration,
                    transcription_id=vt.id,
                    audio_analysis=audio_analysis,
                )
            except Exception:
                logger.exception("Post-transcription auto-edit hook failed for video %s", video_id)

            # The AI creative director, if this video was signed up for one.
            # Deliberately after the auto-edit above: the director reads the
            # video as it will actually be watched, not the uncut take, so it
            # must not run until the cut has been seeded. Never raises — the
            # transcript and the cut are already done and are the valuable part.
            try:
                from app.services.director_trigger import run_post_cut_director

                run_post_cut_director(db, video_id)
            except Exception:
                logger.exception("Post-cut director trigger failed for video %s", video_id)

            try:
                from app.services.repurpose_pipeline import create_clips_for_completed_repurpose_jobs

                create_clips_for_completed_repurpose_jobs(
                    db,
                    video_id,
                    segments=nonempty,
                    video_duration=video_duration,
                )
            except Exception:
                logger.exception("Auto clip creation failed for video %s", video_id)
                try:
                    from app.services.repurpose_pipeline import mark_repurpose_jobs_failed

                    mark_repurpose_jobs_failed(db, video_id, "Auto clip creation failed")
                except Exception:
                    logger.exception("Could not mark auto clip failure for video %s", video_id)

    except TranscriptionTerminalFailure:
        raise
    except subprocess.CalledProcessError as e:
        err = (e.stderr or e.stdout or "")[:4000]
        if _ffmpeg_stderr_suggests_no_audio(err):
            logger.info(
                "Treating ffmpeg failure as no-audio for video %s: %s",
                video_id,
                err[:500],
            )
            try:
                row = (
                    db.query(VideoTranscription)
                    .filter(VideoTranscription.video_id == video_id)
                    .first()
                )
                if row:
                    row.segments = []
                    row.speakers = []
                    row.speaker_count = 0
                    row.status = "failed"
                    row.model_name = _preferred_transcription_model_id(db, video_id)
                    row.error_message = (
                        "ffmpeg found no audio in this stream (often a video-only YouTube URL). "
                        "Use Force new job after updating the worker."
                    )
                    _emit_transcription_failure(db, video_id, "ffmpeg_no_audio")
                    db.commit()
                    from app.services.repurpose_pipeline import mark_repurpose_jobs_failed

                    mark_repurpose_jobs_failed(db, video_id, row.error_message)
            except Exception:
                db.rollback()
                logger.exception("Could not persist no-audio completion for video %s", video_id)
            raise RuntimeError("ffmpeg found no usable audio") from e
        logger.exception("ffmpeg failed for video %s", video_id)
        _fail(db, video_id, f"Audio extraction failed: {err}")
        raise
    except Exception as e:
        logger.exception("Transcription failed for video %s", video_id)
        _fail(db, video_id, str(e)[:4000])
        raise
    finally:
        db.close()


def _fail(db: Session, video_id: int, message: str) -> None:
    try:
        db.rollback()
    except Exception:
        pass
    try:
        row = (
            db.query(VideoTranscription)
            .filter(VideoTranscription.video_id == video_id)
            .first()
        )
        if row:
            row.status = "failed"
            row.error_message = message
            _emit_transcription_failure(db, video_id, "transcription_exception")
            db.commit()
            from app.services.repurpose_pipeline import mark_repurpose_jobs_failed

            mark_repurpose_jobs_failed(db, video_id, message)

            from app.db.models import Video as _Video
            from app.services.realtime import publish_video_update

            uploader_id = (
                db.query(_Video.uploader_id).filter(_Video.id == video_id).scalar()
            )
            publish_video_update(
                uploader_id, video_id, kind="transcription", status="failed"
            )
    except Exception:
        db.rollback()
        logger.exception("Could not persist transcription failure for video %s", video_id)
