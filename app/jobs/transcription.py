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
from app.db.models import RepurposeJob, Video, VideoTranscription

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


def transcribe_video(video_id: int) -> None:
    _ensure_worker_logging()
    logger.info("transcribe_video: starting job for video_id=%s", video_id)
    db: Session = SessionLocal()
    try:
        vt = db.query(VideoTranscription).filter(VideoTranscription.video_id == video_id).first()
        video = db.query(Video).filter(Video.id == video_id).first()
        if not video or not vt:
            logger.error("transcribe_video: missing video or transcription row for id %s", video_id)
            return

        vt.status = "processing"
        vt.error_message = None
        db.commit()

        model_size = os.environ.get("WHISPER_MODEL_SIZE", "base").strip() or "base"
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
                vt.model_name = model_size
                vt.error_message = (
                    "No usable audio extracted (file may be video-only). For YouTube imports, "
                    "re-run transcription with an updated worker that uses a dedicated audio stream."
                )
                db.commit()
                from app.services.repurpose_pipeline import mark_repurpose_jobs_failed

                mark_repurpose_jobs_failed(db, video_id, vt.error_message or "No usable audio extracted")
                return

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
            segments_iter, _info = model.transcribe(str(wav_path), beam_size=beam)

            segments: list[dict] = []
            speaker_labels: set[str] = set()
            seg_count = 0
            for seg in segments_iter:
                # Minimal diarization-compatible shape.
                # We preserve a deterministic speaker tag even before whisperx integration.
                speaker = "SPEAKER_1"
                speaker_labels.add(speaker)
                segments.append(
                    {
                        "start": float(seg.start) + range_start,
                        "end": float(seg.end) + range_start,
                        "text": (seg.text or "").strip(),
                        "speaker": speaker,
                    }
                )
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

            nonempty = [s for s in segments if (s.get("text") or "").strip()]
            if not nonempty:
                vt.segments = []
                vt.speakers = []
                vt.speaker_count = 0
                vt.status = "failed"
                vt.model_name = model_size
                vt.error_message = (
                    "Whisper produced no speech. Common cause: video-only stream with no audio track. "
                    "Use Force new job after updating the worker, or re-import from YouTube."
                )
                db.commit()
                logger.warning("Transcription produced zero segments for video %s", video_id)
                from app.services.repurpose_pipeline import mark_repurpose_jobs_failed

                mark_repurpose_jobs_failed(db, video_id, vt.error_message or "Whisper produced no speech")
                return

            vt.segments = nonempty
            vt.speakers = sorted(speaker_labels)
            vt.speaker_count = len(speaker_labels)
            vt.status = "completed"
            vt.model_name = model_size
            vt.error_message = None
            db.commit()
            logger.info("Transcription completed for video %s (%s segments)", video_id, len(nonempty))
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
                    row.model_name = os.environ.get("WHISPER_MODEL_SIZE", "base").strip() or "base"
                    row.error_message = (
                        "ffmpeg found no audio in this stream (often a video-only YouTube URL). "
                        "Use Force new job after updating the worker."
                    )
                    db.commit()
                    from app.services.repurpose_pipeline import mark_repurpose_jobs_failed

                    mark_repurpose_jobs_failed(db, video_id, row.error_message)
            except Exception:
                db.rollback()
                logger.exception("Could not persist no-audio completion for video %s", video_id)
            return
        logger.exception("ffmpeg failed for video %s", video_id)
        _fail(db, video_id, f"Audio extraction failed: {err}")
    except Exception as e:
        logger.exception("Transcription failed for video %s", video_id)
        _fail(db, video_id, str(e)[:4000])
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
            db.commit()
            from app.services.repurpose_pipeline import mark_repurpose_jobs_failed

            mark_repurpose_jobs_failed(db, video_id, message)
    except Exception:
        db.rollback()
        logger.exception("Could not persist transcription failure for video %s", video_id)
