"""Repurpose job orchestration."""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.db.models import AiResult, Clip, ClipStyle, ClipTemplate, RepurposeJob, Video, VideoTranscription
from app.services.auto_edit import filter_segments_to_ranges
from app.services.clip_analysis import ClipSuggestion, suggest_clips
from app.services.transcription_enqueue import prepare_and_enqueue_transcription

logger = logging.getLogger(__name__)

_VALID_ASPECT_RATIOS = {"9:16", "1:1", "16:9"}
_DEFAULT_CLIP_COUNT = 8

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

    resolved_duration = video_duration if video_duration is not None else _video_duration(video)

    # Auto-edit integration: if the source video has a rough-cut draft with
    # non-trivial keepRanges (auto-seeded or user-edited), keep clip
    # suggestion windows out of the ranges that were cut. No draft, empty
    # ranges, or ranges spanning the whole video (i.e. effectively no cuts) —
    # unchanged legacy behavior.
    kept_ranges = _kept_ranges_for_video(db, job.video_id)
    if kept_ranges and _ranges_are_non_trivial(kept_ranges, resolved_duration):
        filtered_segments = filter_segments_to_ranges(usable_segments, kept_ranges)
        if filtered_segments:
            usable_segments = filtered_segments

    min_duration, max_duration = _duration_bounds(job.clip_length_bucket)
    clip_count = _clip_count_for_job(job)
    aspect_ratios = _aspect_ratios_for_job(job)
    suggestions = suggest_clips(
        usable_segments,
        min_duration=min_duration,
        max_duration=max_duration,
        max_suggestions=clip_count,
        video_duration=resolved_duration,
        focus_prompt=job.clip_anything_prompt if job.clip_mode == "clip_anything" else None,
    )

    if not suggestions:
        job.status = "completed"
        job.created_clip_ids = []
        job.error_message = None
        db.commit()
        return []

    # Key includes aspect_ratio so that fanning the same moment out across
    # multiple aspect ratios doesn't get de-duped against itself.
    existing = {
        (round(float(c.start_time), 2), round(float(c.end_time), 2), c.aspect_ratio)
        for c in db.query(Clip)
        .filter(Clip.video_id == job.video_id, Clip.user_id == job.user_id)
        .all()
    }
    template = _template_for_job(db, job)
    created_ids: list[int] = []
    for suggestion in suggestions:
        for aspect_ratio in aspect_ratios:
            key = (round(float(suggestion.start_time), 2), round(float(suggestion.end_time), 2), aspect_ratio)
            if key in existing:
                continue
            clip = _clip_from_suggestion(job, suggestion, aspect_ratio=aspect_ratio)
            db.add(clip)
            db.flush()
            db.add(_style_for_clip(clip.id, template))
            created_ids.append(clip.id)
            existing.add(key)

    job.created_clip_ids = created_ids
    job.status = "completed"
    job.error_message = None
    db.commit()

    # Fire-and-forget auto-render for every freshly suggested clip so each has
    # its own MP4 + thumbnail instead of falling back to the source video.
    if created_ids:
        try:
            from app.jobs.queue import enqueue_clip_render_job
            from app.services.clip_renderer import fast_thumbnail_for_clip

            for cid in created_ids:
                clip_row = db.query(Clip).filter(Clip.id == cid).first()
                if not clip_row:
                    continue
                fast_thumbnail_for_clip(db, cid)
                job_id = enqueue_clip_render_job(cid)
                if job_id is not None:
                    clip_row.status = "queued"
                    clip_row.rq_job_id = job_id
            db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("auto-render enqueue failed for repurpose job %s", job.id)
            db.rollback()

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


def _clip_from_suggestion(job: RepurposeJob, suggestion: ClipSuggestion, *, aspect_ratio: str) -> Clip:
    start = float(suggestion.start_time)
    end = float(suggestion.end_time)
    return Clip(
        video_id=job.video_id,
        user_id=job.user_id,
        name=_clip_name(suggestion.transcript),
        start_time=start,
        end_time=end,
        cuts=[{"start": start, "end": end}],
        duration_seconds=float(suggestion.duration),
        aspect_ratio=aspect_ratio,
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


def _kept_ranges_for_video(db: Session, video_id: int | None) -> list[dict[str, Any]] | None:
    """The source video's rough-cut draft `keepRanges`, if any (auto-seeded
    by the post-transcription auto-edit hook, or saved by the rough-cut
    editor). Returns None when there is no draft or no ranges, so callers
    can treat that as "unchanged legacy behavior"."""
    if not video_id:
        return None
    row = (
        db.query(AiResult)
        .filter(AiResult.video_id == video_id, AiResult.result_type == "rough_cut_draft")
        .first()
    )
    if not row or not isinstance(row.result_data, dict):
        return None
    ranges = row.result_data.get("keepRanges")
    if not isinstance(ranges, list) or not ranges:
        return None
    return ranges


def _ranges_are_non_trivial(ranges: list[dict[str, Any]], duration: float | None) -> bool:
    """False when keepRanges effectively cover the whole video (no real cuts
    were made, or duration is unknown so triviality can't be verified) —
    the conservative choice that skips filtering rather than risk mutating
    segments for no benefit."""
    if not ranges or not duration or duration <= 0:
        return False
    total = 0.0
    for r in ranges:
        try:
            total += max(0.0, float(r.get("end", 0)) - float(r.get("start", 0)))
        except (TypeError, ValueError, AttributeError):
            continue
    return total < max(duration - 0.5, 0.0)


def _clip_count_for_job(job: RepurposeJob) -> int:
    """Number of suggested MOMENTS to clip (persisted in source_meta by the
    route layer). Falls back to the pipeline's historical default of 8."""
    meta = job.source_meta if isinstance(job.source_meta, dict) else {}
    raw = meta.get("clip_count")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_CLIP_COUNT
    return value if value > 0 else _DEFAULT_CLIP_COUNT


def _aspect_ratios_for_job(job: RepurposeJob) -> list[str]:
    """Aspect ratios to fan each suggested moment out into. `source_meta['aspect_ratios']`
    (persisted by the route layer when the request supplied `aspect_ratios`) wins;
    otherwise fall back to the job's single `aspect_ratio` field, as before."""
    meta = job.source_meta if isinstance(job.source_meta, dict) else {}
    raw = meta.get("aspect_ratios")
    if isinstance(raw, list):
        seen: set[str] = set()
        out: list[str] = []
        for ratio in raw:
            if isinstance(ratio, str) and ratio in _VALID_ASPECT_RATIOS and ratio not in seen:
                seen.add(ratio)
                out.append(ratio)
        if out:
            return out
    return [job.aspect_ratio or "9:16"]


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
