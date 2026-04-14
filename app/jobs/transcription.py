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

import httpx
from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.db.models import Video, VideoTranscription

logger = logging.getLogger(__name__)

# PCM16 mono 16kHz: below ~1s of silence is still > ~32kB; tiny files imply no usable audio.
_MIN_WAV_BYTES_FOR_WHISPER = 8000


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


def _run_ffmpeg_to_wav(input_path: Path, wav_path: Path) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-ar",
        "16000",
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        str(wav_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def transcribe_video(video_id: int) -> None:
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

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            media_path = tmp_path / "input.bin"
            wav_path = tmp_path / "audio.wav"

            with httpx.Client(timeout=httpx.Timeout(600.0, connect=30.0)) as client:
                r = client.get(video.file_path, follow_redirects=True)
                r.raise_for_status()
                media_path.write_bytes(r.content)

            _run_ffmpeg_to_wav(media_path, wav_path)

            wav_size = wav_path.stat().st_size if wav_path.exists() else 0
            if wav_size < _MIN_WAV_BYTES_FOR_WHISPER:
                logger.info(
                    "Skipping Whisper for video %s: WAV too small (%s bytes); likely no audio",
                    video_id,
                    wav_size,
                )
                vt.segments = []
                vt.status = "completed"
                vt.model_name = model_size
                vt.error_message = None
                db.commit()
                return

            from faster_whisper import WhisperModel

            model = WhisperModel(model_size, device=device, compute_type=compute_type)
            segments_iter, _info = model.transcribe(str(wav_path), beam_size=5)

            segments: list[dict] = []
            for seg in segments_iter:
                segments.append(
                    {
                        "start": float(seg.start),
                        "end": float(seg.end),
                        "text": (seg.text or "").strip(),
                    }
                )

            vt.segments = segments
            vt.status = "completed"
            vt.model_name = model_size
            vt.error_message = None
            db.commit()
            logger.info("Transcription completed for video %s (%s segments)", video_id, len(segments))

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
                    row.status = "completed"
                    row.model_name = os.environ.get("WHISPER_MODEL_SIZE", "base").strip() or "base"
                    row.error_message = None
                    db.commit()
            except Exception:
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
    except Exception:
        logger.exception("Could not persist transcription failure for video %s", video_id)
