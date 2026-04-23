"""Repurpose job orchestration."""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.db.models import Clip, ClipStyle, ClipTemplate, RepurposeJob, Video, VideoTranscription
from app.services.clip_analysis import ClipSuggestion, suggest_clips
from app.services.transcription_enqueue import prepare_and_enqueue_transcription

logger = logging.getLogger(__name__)

_LENGTH_BUCKETS: dict[str, tuple[float, float]] = {
    "lt_30": (8.0, 30.0),
    "30_59": (30.0, 59.0),
    "60_89": (60.0, 89.0),
    "90_180": (90.0, 180.0),
    "180_300": (180.0, 300.0),
    "300_600": (300.0, 600.0),
    "600_900": (600.0, 900.0),
}


def start_repurpose_processing(db: Session, job_id: int) -> None:
    job = db.query(RepurposeJob).filter(RepurposeJob.id == job_id).first()
    if not job or not job.video_id:
        return

    vt = (
        db.query(VideoTranscription)
        .filter(VideoTranscription.video_id == job.video_id)
        .first()
    )
    if vt and vt.status == "completed" and vt.segments:
        create_clips_for_repurpose_job(db, job.id)
        return

    job.status = "processing"
    job.error_message = None
    db.commit()

    if vt and vt.status in ("queued", "processing"):
        return

    try:
        prepare_and_enqueue_transcription(db, job.video_id)
    except HTTPException as exc:
        if exc.status_code == 409:
            _mark_job_processing(db, job.id)
            return
        _mark_job_failed(db, job.id, str(exc.detail))
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning("Repurpose transcription enqueue failed for job %s: %s", job.id, exc)
        _mark_job_failed(db, job.id, str(exc)[:4000])
        return

    vt = (
        db.query(VideoTranscription)
        .filter(VideoTranscription.video_id == job.video_id)
        .first()
    )
    if vt and vt.status == "failed":
        _mark_job_failed(db, job.id, vt.error_message or "Transcription could not be queued")


def create_clips_for_completed_repurpose_jobs(
    db: Session,
    video_id: int,
    *,
    segments: list[dict[str, Any]] | None = None,
    video_duration: float | None = None,
) -> None:
    jobs = (
        db.query(RepurposeJob)
        .filter(RepurposeJob.video_id == video_id)
        .order_by(RepurposeJob.id.asc())
        .all()
    )
    for job in jobs:
        if job.status == "completed" and job.created_clip_ids:
            continue
        create_clips_for_repurpose_job(
            db,
            job.id,
            segments=segments,
            video_duration=video_duration,
        )


def mark_repurpose_jobs_failed(db: Session, video_id: int, message: str) -> None:
    try:
        db.rollback()
    except Exception:
        pass
    try:
        jobs = (
            db.query(RepurposeJob)
            .filter(RepurposeJob.video_id == video_id)
            .all()
        )
        for job in jobs:
            if job.status != "completed":
                job.status = "failed"
                job.error_message = message[:4000]
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Could not mark repurpose jobs failed for video %s", video_id)


def create_clips_for_repurpose_job(
    db: Session,
    job_id: int,
    *,
    segments: list[dict[str, Any]] | None = None,
    video_duration: float | None = None,
) -> list[int]:
    job = db.query(RepurposeJob).filter(RepurposeJob.id == job_id).first()
    if not job or not job.video_id:
        return []

    video = db.query(Video).filter(Video.id == job.video_id).first()
    if not video:
        _mark_job_failed(db, job.id, "Source video not found")
        return []

    if segments is None:
        vt = (
            db.query(VideoTranscription)
            .filter(VideoTranscription.video_id == job.video_id)
            .first()
        )
        segments = list(vt.segments or []) if vt else []

    range_start, range_end = _range_for_job(job)
    usable_segments = _segments_for_job(segments, range_start, range_end)
    if not usable_segments:
        _mark_job_failed(db, job.id, "No transcript segments available for clipping")
        return []

    min_duration, max_duration = _duration_bounds(job.clip_length_bucket)
    suggestions = suggest_clips(
        usable_segments,
        min_duration=min_duration,
        max_duration=max_duration,
        max_suggestions=8,
        video_duration=video_duration if video_duration is not None else _video_duration(video),
        focus_prompt=job.clip_anything_prompt if job.clip_mode == "clip_anything" else None,
    )

    if not suggestions:
        job.status = "completed"
        job.created_clip_ids = []
        job.error_message = None
        db.commit()
        return []

    existing = {
        (round(float(c.start_time), 2), round(float(c.end_time), 2))
        for c in db.query(Clip)
        .filter(Clip.video_id == job.video_id, Clip.user_id == job.user_id)
        .all()
    }
    template = _template_for_job(db, job)
    created_ids: list[int] = []
    for suggestion in suggestions:
        key = (round(float(suggestion.start_time), 2), round(float(suggestion.end_time), 2))
        if key in existing:
            continue
        clip = _clip_from_suggestion(job, suggestion)
        db.add(clip)
        db.flush()
        db.add(_style_for_clip(clip.id, template))
        created_ids.append(clip.id)
        existing.add(key)

    job.created_clip_ids = created_ids
    job.status = "completed"
    job.error_message = None
    db.commit()
    return created_ids


def _mark_job_processing(db: Session, job_id: int) -> None:
    try:
        db.rollback()
    except Exception:
        pass
    job = db.query(RepurposeJob).filter(RepurposeJob.id == job_id).first()
    if job:
        job.status = "processing"
        job.error_message = None
        db.commit()


def _mark_job_failed(db: Session, job_id: int, message: str) -> None:
    try:
        db.rollback()
    except Exception:
        pass
    try:
        job = db.query(RepurposeJob).filter(RepurposeJob.id == job_id).first()
        if job:
            job.status = "failed"
            job.error_message = message[:4000]
            db.commit()
    except Exception:
        db.rollback()
        logger.exception("Could not mark repurpose job %s failed", job_id)


def _clip_from_suggestion(job: RepurposeJob, suggestion: ClipSuggestion) -> Clip:
    return Clip(
        video_id=job.video_id,
        user_id=job.user_id,
        name=_clip_name(suggestion.transcript),
        start_time=float(suggestion.start_time),
        end_time=float(suggestion.end_time),
        duration_seconds=float(suggestion.duration),
        aspect_ratio=job.aspect_ratio or "9:16",
        virality_score=float(suggestion.virality_score),
        status="draft",
        is_ai_suggested=True,
        suggestion_reason=suggestion.reason,
        hooks_matched=suggestion.hooks_matched,
        transcript_text=suggestion.transcript,
    )


def _style_for_clip(clip_id: int, template: ClipTemplate | None) -> ClipStyle:
    style = ClipStyle(clip_id=clip_id)
    if not template:
        return style
    for key, value in (template.style_config or {}).items():
        if hasattr(style, key):
            setattr(style, key, value)
    return style


def _template_for_job(db: Session, job: RepurposeJob) -> ClipTemplate | None:
    if not job.subtitle_template_id:
        return None
    return (
        db.query(ClipTemplate)
        .filter(
            ClipTemplate.id == job.subtitle_template_id,
            (ClipTemplate.user_id == job.user_id)
            | (ClipTemplate.user_id.is_(None))
            | (ClipTemplate.is_public.is_(True)),
        )
        .first()
    )


def _segments_for_job(
    segments: list[dict[str, Any]],
    range_start: float,
    range_end: float | None,
) -> list[dict[str, Any]]:
    if range_start <= 0 and range_end is None:
        return segments
    out: list[dict[str, Any]] = []
    for segment in segments:
        try:
            start = float(segment.get("start", 0.0))
            end = float(segment.get("end", start))
            if end > range_start and (range_end is None or start < range_end):
                out.append(segment)
        except (TypeError, ValueError):
            continue
    return out


def _range_for_job(job: RepurposeJob) -> tuple[float, float | None]:
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


def _duration_bounds(bucket: str | None) -> tuple[float, float]:
    return _LENGTH_BUCKETS.get(bucket or "", _LENGTH_BUCKETS["lt_30"])


def _video_duration(video: Video) -> float | None:
    try:
        return float(video.duration) if video.duration else None
    except (TypeError, ValueError):
        return None


def _clip_name(transcript: str) -> str:
    text = re.sub(r"\s+", " ", (transcript or "").strip())
    if not text:
        return "AI clip"
    return text[:72].rstrip(" ,.;:-") or "AI clip"
