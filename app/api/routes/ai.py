from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import AiResult, Comment, Project, Video, VideoTranscription, User
from app.jobs.ai_jobs import (
    build_briefing_digest,
    build_video_metadata,
    detect_chapters,
    detect_fillers,
)
from app.jobs.queue import enqueue_ai_review_job
from app.services.ai_client import generate_broll_image, generate_json
from app.services.auto_edit import (
    AutoEditOptions,
    _analyze_segments,
)
from app.services.video_review import build_review, empty_review
from app.utils.cloudinary import upload_image_bytes
from app.utils.security import get_current_user
from app.services.project_access import can_access_project

router = APIRouter(
    prefix="/videos",
    tags=["AI Features"],
)


def _check_video_access(video_id: int, db: Session, current_user: User) -> Video:
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    project = db.query(Project).filter(Project.id == video.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_access_project(db, current_user.id, project):
        raise HTTPException(status_code=403, detail="Not authorized to access this video")
    return video


def _upsert_result(
    db: Session, video_id: int, result_type: str, data: dict, status: str = "completed"
) -> dict:
    row = (
        db.query(AiResult)
        .filter(AiResult.video_id == video_id, AiResult.result_type == result_type)
        .first()
    )
    if row is None:
        row = AiResult(video_id=video_id, result_type=result_type)
        db.add(row)
    row.status = status
    row.error_message = None
    row.result_data = data
    db.commit()
    db.refresh(row)
    return {
        "video_id": row.video_id,
        "result_type": row.result_type,
        "status": row.status,
        "result_data": row.result_data,
    }


@router.post("/{video_id}/ai/generate-metadata")
def generate_metadata(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_access(video_id, db, current_user)
    result = build_video_metadata(video_id)
    return _upsert_result(db, video_id, "metadata", result)


@router.post("/{video_id}/ai/briefing-digest")
def briefing_digest(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_access(video_id, db, current_user)
    result = build_briefing_digest(video_id)
    return _upsert_result(db, video_id, "briefing_digest", result)


@router.post("/{video_id}/ai/chapters")
def chapters(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_access(video_id, db, current_user)
    result = detect_chapters(video_id)
    return _upsert_result(db, video_id, "chapters", result)


@router.post("/{video_id}/ai/detect-fillers")
def fillers(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_access(video_id, db, current_user)
    result = detect_fillers(video_id)
    return _upsert_result(db, video_id, "fillers", result)


@router.post("/{video_id}/ai/captions/generate")
def generate_captions(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_access(video_id, db, current_user)
    tr = db.query(VideoTranscription).filter(VideoTranscription.video_id == video_id).first()
    segments = tr.segments if tr and tr.segments else []
    lines = []
    for idx, seg in enumerate(segments, start=1):
        start = float(seg.get("start", 0))
        end = float(seg.get("end", start))
        text = str(seg.get("text", "")).strip()
        lines.append({"id": idx, "start": start, "end": end, "text": text})
    return _upsert_result(
        db,
        video_id,
        "captions",
        {"format": "vtt", "style": {}, "segments": lines, "burned_video_url": None},
    )


@router.get("/{video_id}/ai/captions")
def get_captions(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_access(video_id, db, current_user)
    row = (
        db.query(AiResult)
        .filter(AiResult.video_id == video_id, AiResult.result_type == "captions")
        .first()
    )
    if row is None:
        return {"video_id": video_id, "result_type": "captions", "status": "pending", "result_data": None}
    return {
        "video_id": row.video_id,
        "result_type": row.result_type,
        "status": row.status,
        "result_data": row.result_data,
    }


@router.put("/{video_id}/ai/captions")
def update_captions(
    video_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_access(video_id, db, current_user)
    return _upsert_result(
        db,
        video_id,
        "captions",
        {
            "format": body.get("format", "vtt"),
            "style": body.get("style", {}),
            "segments": body.get("segments", []),
            "burned_video_url": body.get("burned_video_url"),
        },
    )


@router.post("/{video_id}/ai/captions/export")
def export_captions(
    video_id: int,
    body: dict | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_access(video_id, db, current_user)
    existing = (
        db.query(AiResult)
        .filter(AiResult.video_id == video_id, AiResult.result_type == "captions")
        .first()
    )
    data = existing.result_data if existing and existing.result_data else {}
    data["burned_video_url"] = body.get("burned_video_url") if body else None
    data["export_status"] = "queued"
    return _upsert_result(db, video_id, "captions", data)


@router.post("/{video_id}/ai/translate")
def translate_subtitles(
    video_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_access(video_id, db, current_user)
    target_language = str(body.get("target_language", "en")).lower()
    tr = db.query(VideoTranscription).filter(VideoTranscription.video_id == video_id).first()
    segments = tr.segments if tr and tr.segments else []
    fallback = {"language": target_language, "segments": segments}
    prompt = (
        "Translate these transcript segments while preserving start/end.\n"
        "Return JSON with shape: {language: string, segments: [{start,end,text}]}\n\n"
        f"target_language={target_language}\nsegments={segments}"
    )
    translated = generate_json(prompt, fallback=fallback)
    return _upsert_result(db, video_id, f"translation_{target_language}", translated)


@router.post("/{video_id}/ai/chat")
def ai_chat(
    video_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_access(video_id, db, current_user)
    message = str(body.get("message", "")).strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    comments = (
        db.query(Comment)
        .filter(Comment.video_id == video_id)
        .order_by(Comment.timecode.asc())
        .limit(120)
        .all()
    )
    tr = db.query(VideoTranscription).filter(VideoTranscription.video_id == video_id).first()
    segments = tr.segments if tr and tr.segments else []
    context = {
        "transcript": segments[:400],
        "comments": [{"timecode": c.timecode, "text": c.text} for c in comments],
        "question": message,
    }
    answer = generate_json(
        "Answer the question from transcript/comments context. "
        "Return JSON: {answer:string,timecodes:number[]}\n"
        f"{context}",
        fallback={"answer": "I could not derive a reliable answer yet.", "timecodes": []},
    )
    return _upsert_result(db, video_id, "chat_last", answer)


@router.post("/{video_id}/ai/summarize")
def summarize_video(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_access(video_id, db, current_user)
    tr = db.query(VideoTranscription).filter(VideoTranscription.video_id == video_id).first()
    segments = tr.segments if tr and tr.segments else []
    fallback = {"summary": "", "highlights": []}
    result = generate_json(
        "Create a short summary and 3-5 highlight ranges from transcript. "
        "Return JSON: {summary:string,highlights:[{start,end,reason}]}\n"
        f"segments={segments}",
        fallback=fallback,
    )
    return _upsert_result(db, video_id, "summary", result)


@router.post("/{video_id}/ai/broll-suggestions")
def broll(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_access(video_id, db, current_user)
    tr = db.query(VideoTranscription).filter(VideoTranscription.video_id == video_id).first()
    segments = tr.segments if tr and tr.segments else []
    result = generate_json(
        "Suggest B-roll opportunities from transcript. "
        "Return JSON {suggestions:[{start,end,description,suggested_keywords}]}\n"
        f"segments={segments}",
        fallback={"suggestions": []},
    )
    return _upsert_result(db, video_id, "broll", result)


@router.post("/{video_id}/ai/thumbnails")
def thumbnails(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_access(video_id, db, current_user)
    # Placeholder scores; extraction/vision scoring is implementation-ready in API shape.
    data = {
        "thumbnails": [
            {"timestamp": 5, "score": 0.8, "reason": "Early hook frame"},
            {"timestamp": 15, "score": 0.7, "reason": "Face-centered frame"},
            {"timestamp": 30, "score": 0.75, "reason": "High contrast frame"},
        ]
    }
    return _upsert_result(db, video_id, "thumbnails", data)


@router.post("/{video_id}/ai/remove-fillers")
def remove_fillers(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_access(video_id, db, current_user)
    return _upsert_result(db, video_id, "remove_fillers_job", {"status": "queued"}, status="queued")


class RoughCutRequest(AutoEditOptions):
    brief: str = ""
    clip_ids: list[int] = Field(default_factory=list)


class AutoEditPrefsBody(AutoEditOptions):
    """Auto-edit prefs captured at project/video creation time (create-project
    wizard "Auto edit" card). `enabled` gates whether the post-transcription
    hook (app.services.auto_edit.run_post_transcription_auto_edit) runs
    analysis at all; `auto_apply` additionally gates whether that analysis's
    keepRanges get written into the rough-cut draft vs. only surfaced as
    `aiAnalysis` suggestions for the user to review first.
    """

    enabled: bool = False
    auto_apply: bool = False
    source_range_start_seconds: float | None = Field(default=None, ge=0)
    source_range_end_seconds: float | None = Field(default=None, ge=0)


@router.put("/{video_id}/ai/auto-edit-prefs")
def save_auto_edit_prefs(
    video_id: int,
    body: AutoEditPrefsBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_access(video_id, db, current_user)
    result = _upsert_result(
        db,
        video_id,
        "auto_edit_prefs",
        body.model_dump(mode="json"),
        status="completed",
    )
    if body.enabled:
        transcription = (
            db.query(VideoTranscription)
            .filter(VideoTranscription.video_id == video_id)
            .first()
        )
        if transcription and transcription.status == "completed" and transcription.segments:
            from app.services.auto_edit import run_post_transcription_auto_edit

            video = db.query(Video).filter(Video.id == video_id).first()
            run_post_transcription_auto_edit(
                db,
                video_id,
                segments=list(transcription.segments),
                video_duration=float(video.duration) if video and video.duration else None,
                transcription_id=transcription.id,
            )
    return result


@router.get("/{video_id}/ai/auto-edit-prefs")
def get_auto_edit_prefs(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_access(video_id, db, current_user)
    row = (
        db.query(AiResult)
        .filter(AiResult.video_id == video_id, AiResult.result_type == "auto_edit_prefs")
        .first()
    )
    if row is None:
        return {
            "video_id": video_id,
            "result_type": "auto_edit_prefs",
            "status": "pending",
            "result_data": AutoEditPrefsBody().model_dump(mode="json"),
        }
    return {
        "video_id": row.video_id,
        "result_type": row.result_type,
        "status": row.status,
        "result_data": row.result_data,
    }


@router.post("/{video_id}/ai/rough-cut")
def rough_cut(
    video_id: int,
    body: RoughCutRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    video = _check_video_access(video_id, db, current_user)
    tr = db.query(VideoTranscription).filter(VideoTranscription.video_id == video_id).first()
    segments = list(tr.segments) if tr and tr.segments else []
    duration = float(getattr(video, "duration", 0) or 0)
    analysis = _analyze_segments(
        segments,
        duration,
        remove_fillers=body.remove_fillers,
        remove_silences=body.remove_silences,
        remove_bad_takes=body.remove_bad_takes,
        aggressiveness=body.aggressiveness,
    )
    return _upsert_result(
        db,
        video_id,
        "rough_cut",
        {**analysis, "brief": body.brief, "clip_ids": body.clip_ids},
        status="completed",
    )


def _review_row(db: Session, video_id: int) -> dict:
    row = (
        db.query(AiResult)
        .filter(AiResult.video_id == video_id, AiResult.result_type == "review")
        .first()
    )
    if row is None:
        return {
            "video_id": video_id,
            "result_type": "review",
            "status": "idle",
            "result_data": None,
            "error_message": None,
            "updated_at": None,
        }
    return {
        "video_id": row.video_id,
        "result_type": row.result_type,
        "status": row.status,
        "result_data": row.result_data,
        "error_message": row.error_message,
        "updated_at": row.updated_at,
    }


@router.post("/{video_id}/ai/review")
def review_video(
    video_id: int,
    body: AutoEditOptions | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Kick off the pro-tier AI review: engagement + per-dimension scores and
    timestamped notes, judged from the transcript *and* sampled frames.

    Frame extraction is far too slow for a request, so this enqueues the work
    and returns the row as ``processing``; the player polls
    ``GET /videos/{id}/ai/review``. Without ``REDIS_URL`` (dev, no worker) the
    review runs inline so the feature still works.
    """
    video = _check_video_access(video_id, db, current_user)
    tr = db.query(VideoTranscription).filter(VideoTranscription.video_id == video_id).first()
    segments = list(tr.segments) if tr and tr.segments else []
    opts = body or AutoEditOptions()

    if not segments:
        return _upsert_result(
            db,
            video_id,
            "review",
            empty_review("Transcribe the video first to run an AI review."),
        )

    job_id = enqueue_ai_review_job(video_id, opts.model_dump())
    if job_id:
        previous = _review_row(db, video_id)
        pending = previous.get("result_data")
        # Keep the last report on screen while the new one builds, so the panel
        # doesn't flash empty on a re-review.
        return _upsert_result(
            db,
            video_id,
            "review",
            pending if isinstance(pending, dict) else empty_review(""),
            status="processing",
        )

    comments = (
        db.query(Comment)
        .filter(Comment.video_id == video_id)
        .order_by(Comment.timecode.asc())
        .limit(60)
        .all()
    )
    payload = build_review(
        video_id=video_id,
        duration=float(getattr(video, "duration", 0) or 0),
        media_src=str(getattr(video, "file_path", "") or ""),
        segments=segments,
        comments=[{"timecode": c.timecode, "text": c.text} for c in comments],
        options=opts,
    )
    return _upsert_result(db, video_id, "review", payload)


@router.get("/{video_id}/ai/review")
def get_video_review(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """The current review row — polled while a review job is running."""
    _check_video_access(video_id, db, current_user)
    return _review_row(db, video_id)


@router.get("/{video_id}/ai/rough-cut-draft")
def get_rough_cut_draft(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_access(video_id, db, current_user)
    row = (
        db.query(AiResult)
        .filter(AiResult.video_id == video_id, AiResult.result_type == "rough_cut_draft")
        .first()
    )
    if row is None:
        return {
            "video_id": video_id,
            "result_type": "rough_cut_draft",
            "status": "pending",
            "result_data": None,
        }
    return {
        "video_id": row.video_id,
        "result_type": row.result_type,
        "status": row.status,
        "result_data": row.result_data,
        "error_message": row.error_message,
        "updated_at": row.updated_at,
    }


class RoughCutDraftBody(BaseModel):
    """Matches editube-frontend rough-cut `draftPayload` plus forward-compatible extras."""

    model_config = ConfigDict(extra="allow")

    keepRanges: list[dict[str, Any]] = Field(default_factory=list)
    markers: list[dict[str, Any]] = Field(default_factory=list)
    segments: list[dict[str, Any]] = Field(default_factory=list)
    captionStyle: dict[str, Any] = Field(default_factory=dict)
    layoutStyle: dict[str, Any] = Field(default_factory=dict)
    musicStyle: dict[str, Any] = Field(default_factory=dict)
    brandStyle: dict[str, Any] = Field(default_factory=dict)
    textOverlay: dict[str, Any] = Field(default_factory=dict)
    lowerThirds: list[dict[str, Any]] = Field(default_factory=list)
    trackState: dict[str, Any] = Field(default_factory=dict)
    clipAttributes: dict[str, Any] = Field(default_factory=dict)
    selectedClipTarget: dict[str, Any] | None = None
    effectJobs: list[dict[str, Any]] = Field(default_factory=list)
    wordHighlights: dict[str, Any] = Field(default_factory=dict)
    wordFormats: dict[str, Any] = Field(default_factory=dict)
    mediaName: str | None = None
    duration: float | None = None
    media: dict[str, Any] | None = None
    roughCutBrief: str = ""
    aiAnalysis: dict[str, Any] = Field(default_factory=dict)
    showFillers: bool | None = None
    removeSilence: bool | None = None
    smoothSpeech: bool | None = None
    timelineZoom: float | None = None
    timelineViewportStart: float | None = None
    exportSettings: dict[str, Any] = Field(default_factory=dict)
    activeTab: str | None = None
    transcriptOpen: bool | None = None
    inspectorOpen: bool | None = None
    updatedAt: str | None = None
    magneticTimeline: bool | None = None


@router.put("/{video_id}/ai/rough-cut-draft")
def save_rough_cut_draft(
    video_id: int,
    body: RoughCutDraftBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_access(video_id, db, current_user)
    return _upsert_result(
        db,
        video_id,
        "rough_cut_draft",
        body.model_dump(mode="json", exclude_none=False),
        status="completed",
    )


class RoughCutEffectBody(BaseModel):
    effect_type: str
    clip_key: str
    clip_target: dict[str, Any] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)


class SegmentPreviewBody(BaseModel):
    """One-frame preview request. `selection` is the editor's stored shape."""

    at_seconds: float = 0.0
    selection: dict[str, Any] = Field(default_factory=dict)
    quality: str = "faster"
    #: The clip's `removeBg` attributes, so the preview applies the same invert /
    #: grow / feather / strength the export will. Sent whole rather than as named
    #: fields so adding a control does not require changing this contract.
    refine: dict[str, Any] = Field(default_factory=dict)


@router.post("/{video_id}/ai/segment-preview")
def segment_preview(
    video_id: int,
    body: SegmentPreviewBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Segments one frame from the user's clicks and returns the mask.

    Synchronous on purpose. This answers a click, so it has to return in about
    the time a click takes to feel acknowledged — a queued job plus polling
    cannot. The whole-clip run stays on the queue where it belongs.

    Returns the mask as a base64 greyscale PNG rather than a URL: it is a few KB,
    it is scratch data that would only litter storage, and inlining it means the
    overlay appears with the response instead of after a second fetch.
    """
    video = _check_video_access(video_id, db, current_user)

    from app.jobs.rough_cut_effect import _resolve_media_source
    from app.services.segmentation.base import SegmentationError
    from app.services.segmentation.local import LocalSegmentationProvider
    from app.services.segmentation.preview import preview_mask_png

    prompts = LocalSegmentationProvider._selection_prompts({"selection": body.selection})
    if prompts is None:
        raise HTTPException(status_code=400, detail="Click the subject to select it first.")
    points, labels = prompts

    quality = "better" if str(body.quality).strip().lower() == "better" else "faster"

    try:
        png, width, height = preview_mask_png(
            _resolve_media_source(video.file_path),
            float(body.at_seconds or 0.0),
            points,
            labels,
            quality=quality,
            # Invert / grow / feather come from the same stored attributes the
            # export reads, so the preview shows the refined matte, not a raw one.
            settings=body.refine,
        )
    except SegmentationError as exc:
        # 422, not 500: these are all "the request cannot be satisfied as asked"
        # — model missing, unreadable frame, nothing selected — and the editor
        # shows the message to the user rather than logging a server fault.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    import base64

    return {
        "width": width,
        "height": height,
        "mask_png": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
        "point_count": len(points),
    }


@router.post("/{video_id}/ai/rough-cut-effect")
def start_rough_cut_effect(
    video_id: int,
    body: RoughCutEffectBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_access(video_id, db, current_user)
    effect_type = re.sub(r"[^a-z0-9_-]+", "_", (body.effect_type or "").strip().lower()).strip("_")
    if not effect_type:
        raise HTTPException(status_code=400, detail="effect_type is required")
    if not body.clip_key.strip():
        raise HTTPException(status_code=400, detail="clip_key is required")

    payload = {
        "effectType": effect_type,
        "clipKey": body.clip_key,
        "clipTarget": body.clip_target,
        "settings": body.settings,
        "status": "queued",
        "progress": 0,
    }
    row = AiResult(video_id=video_id, result_type="rough_cut_effect", status="queued", result_data=payload)
    db.add(row)
    db.commit()
    db.refresh(row)

    from app.jobs.queue import enqueue_rough_cut_effect_job

    rq_job_id = enqueue_rough_cut_effect_job(row.id)
    data = dict(row.result_data or {})
    if rq_job_id:
        data["rqJobId"] = rq_job_id
        row.result_data = data
        db.commit()
    else:
        msg = "Background worker unavailable. Configure Redis and an RQ worker for rough-cut effects."
        row.status = "failed"
        row.error_message = msg
        data["status"] = "failed"
        data["error"] = msg
        row.result_data = data
        db.commit()
    db.refresh(row)
    return {
        "video_id": row.video_id,
        "result_type": row.result_type,
        "status": row.status,
        "result_data": row.result_data,
        "error_message": row.error_message,
        "ai_result_id": row.id,
    }


@router.get("/{video_id}/ai/rough-cut-effect/{result_id}")
def get_rough_cut_effect(
    video_id: int,
    result_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_access(video_id, db, current_user)
    row = (
        db.query(AiResult)
        .filter(AiResult.id == result_id, AiResult.video_id == video_id, AiResult.result_type == "rough_cut_effect")
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Effect job not found")

    _reconcile_dead_effect(db, row)

    return {
        "video_id": row.video_id,
        "result_type": row.result_type,
        "status": row.status,
        "result_data": row.result_data,
        "error_message": row.error_message,
        "ai_result_id": row.id,
    }


def _reconcile_dead_effect(db: Session, row: AiResult) -> None:
    """Marks a row failed when the RQ job behind it is gone.

    Without this a job whose worker died stays `processing` forever and the
    editor polls a percentage that will never move again — which is exactly what
    happened when the work-horse was killed by a signal: RQ marked *its* job
    failed, but nothing told our row, so three of them sat at 8% indefinitely.

    Deliberately keyed on the RQ job's actual state rather than a timeout, so a
    genuinely slow job is never killed off for being slow. If the job id is
    missing or Redis is unreachable, this does nothing — a wrong "failed" is
    worse than a stale "processing".
    """
    if row.status not in {"queued", "processing"}:
        return

    payload = dict(row.result_data or {})
    rq_job_id = str(payload.get("rqJobId") or "").strip()
    if not rq_job_id:
        return

    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        return

    try:
        from redis import Redis
        from rq.job import Job

        connection = Redis.from_url(url)
        try:
            job = Job.fetch(rq_job_id, connection=connection)
        except Exception:
            # Job.fetch raises NoSuchJobError once the job has expired out of
            # Redis. A row still claiming to run then has nothing behind it.
            job = None
    except Exception:
        # Redis unreachable or rq unavailable: leave the row alone.
        return

    if job is not None and job.get_status(refresh=True) not in {"failed", "canceled", "stopped"}:
        return

    # Two fields, deliberately. `error` is for the user and says what to do;
    # `errorDetail` is the machine text, kept out of the way behind a disclosure
    # in the editor. Concatenating them put "waitpid returned 6 (signal 6)" in
    # front of someone trying to edit a video, which tells them nothing they can
    # act on.
    reason = "The background worker stopped before finishing. Nothing was changed — try again."
    detail = None
    if job is not None:
        lines = (job.exc_info or "").strip().splitlines()
        if lines:
            detail = lines[-1][:300]

    payload["status"] = "failed"
    if detail:
        payload["errorDetail"] = detail
    # Zeroed so the row and the draft agree; a failed job showing 8% invites the
    # reading that it is still partway through.
    payload["progress"] = 0
    payload["error"] = reason
    row.status = "failed"
    row.error_message = reason
    row.result_data = payload
    db.commit()

    clip_key = str(payload.get("clipKey") or "").strip()
    effect_type = str(payload.get("effectType") or "").strip()
    if clip_key and effect_type:
        # The editor reads progress off the draft, so the draft has to learn too
        # or the inspector keeps showing a spinner for a dead job.
        from app.jobs.rough_cut_effect import _attach_to_draft

        _attach_to_draft(
            db,
            row.video_id,
            clip_key,
            effect_type,
            {
                "resultId": row.id,
                "status": "failed",
                "progress": 0,
                "error": reason,
                **({"errorDetail": detail} if detail else {}),
            },
        )


@router.post("/{video_id}/ai/rough-cut-effect/{result_id}/cancel")
def cancel_rough_cut_effect(
    video_id: int,
    result_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_access(video_id, db, current_user)
    row = (
        db.query(AiResult)
        .filter(AiResult.id == result_id, AiResult.video_id == video_id, AiResult.result_type == "rough_cut_effect")
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Effect job not found")
    data = dict(row.result_data or {})
    rq_job_id = data.get("rqJobId")
    if isinstance(rq_job_id, str) and rq_job_id.strip():
        try:
            from redis import Redis
            from rq.job import Job

            url = os.environ.get("REDIS_URL", "").strip()
            if url:
                job = Job.fetch(rq_job_id, connection=Redis.from_url(url))
                if job.get_status() in ("queued", "scheduled", "deferred"):
                    job.cancel()
        except Exception:
            pass
    if row.status in ("queued", "processing"):
        row.status = "failed"
        row.error_message = "Effect cancelled."
        data["status"] = "failed"
        data["error"] = "Effect cancelled."
        data["progress"] = 0
        row.result_data = data
        db.commit()
    return {"ok": True, "video_id": video_id, "ai_result_id": result_id}


class MaskTrackBody(BaseModel):
    mask: dict[str, Any]
    clip_key: str
    clip_start: float = 0.0
    clip_end: float = 0.0
    anchor_time: float = 0.0
    direction: str = "both"
    source_url: str | None = None


@router.post("/{video_id}/ai/mask-track")
def start_mask_track(
    video_id: int,
    body: MaskTrackBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_access(video_id, db, current_user)
    if not body.clip_key.strip():
        raise HTTPException(status_code=400, detail="clip_key is required")
    if not isinstance(body.mask, dict) or not body.mask:
        raise HTTPException(status_code=400, detail="mask is required")
    direction = (body.direction or "both").strip().lower()
    if direction not in ("forward", "backward", "both"):
        raise HTTPException(status_code=400, detail="direction must be forward, backward, or both")

    # I9: `mask` arrives straight from the request body and previously only
    # got an `isinstance(dict)` check -- unclamped values reached
    # `transform_to_bbox`/`tracker.init` in the RQ job and an unbounded
    # payload was persisted into `AiResult.result_data` and reflected back to
    # clients. `sanitize_mask` (already used by the export path) clamps
    # numerics and caps array sizes; run it here too, before the mask is
    # used OR persisted.
    from app.services.mask_matte import sanitize_mask

    clean_mask = sanitize_mask(body.mask)
    if clean_mask is None:
        raise HTTPException(status_code=400, detail="mask is invalid")
    # Belt-and-suspenders cap on the persisted payload size -- sanitize_mask
    # already bounds array lengths, but this guards the JSON blob we commit
    # into AiResult.result_data (and echo back to clients) against still
    # being unreasonably large.
    if len(json.dumps(clean_mask)) > 512_000:
        raise HTTPException(status_code=400, detail="mask payload is too large")

    payload = {
        "mask": clean_mask,
        "clipKey": body.clip_key,
        "clipStart": body.clip_start,
        "clipEnd": body.clip_end,
        "anchorTime": body.anchor_time,
        "direction": direction,
        "sourceUrl": body.source_url,
        "status": "queued",
        "progress": 0,
    }
    row = AiResult(video_id=video_id, result_type="mask_track", status="queued", result_data=payload)
    db.add(row)
    db.commit()
    db.refresh(row)

    from app.jobs.queue import enqueue_mask_track_job

    rq_job_id = enqueue_mask_track_job(row.id)
    data = dict(row.result_data or {})
    if rq_job_id:
        data["rqJobId"] = rq_job_id
        row.result_data = data
        db.commit()
    else:
        msg = "Background worker unavailable. Configure Redis and an RQ worker for mask tracking."
        row.status = "failed"
        row.error_message = msg
        data["status"] = "failed"
        data["error"] = msg
        row.result_data = data
        db.commit()
    db.refresh(row)
    return {
        "video_id": row.video_id,
        "result_type": row.result_type,
        "status": row.status,
        "result_data": row.result_data,
        "error_message": row.error_message,
        "ai_result_id": row.id,
    }


@router.get("/{video_id}/ai/mask-track/{result_id}")
def get_mask_track(
    video_id: int,
    result_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_access(video_id, db, current_user)
    row = (
        db.query(AiResult)
        .filter(AiResult.id == result_id, AiResult.video_id == video_id, AiResult.result_type == "mask_track")
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Mask track job not found")
    return {
        "video_id": row.video_id,
        "result_type": row.result_type,
        "status": row.status,
        "result_data": row.result_data,
        "error_message": row.error_message,
        "ai_result_id": row.id,
    }


@router.post("/{video_id}/ai/mask-track/{result_id}/cancel")
def cancel_mask_track(
    video_id: int,
    result_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_access(video_id, db, current_user)
    row = (
        db.query(AiResult)
        .filter(AiResult.id == result_id, AiResult.video_id == video_id, AiResult.result_type == "mask_track")
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Mask track job not found")
    data = dict(row.result_data or {})
    rq_job_id = data.get("rqJobId")
    if isinstance(rq_job_id, str) and rq_job_id.strip():
        try:
            from redis import Redis
            from rq.job import Job

            url = os.environ.get("REDIS_URL", "").strip()
            if url:
                job = Job.fetch(rq_job_id, connection=Redis.from_url(url))
                if job.get_status() in ("queued", "scheduled", "deferred"):
                    job.cancel()
        except Exception:
            pass
    if row.status in ("queued", "processing"):
        row.status = "failed"
        row.error_message = "Mask tracking cancelled."
        data["status"] = "failed"
        data["error"] = "Mask tracking cancelled."
        data["progress"] = 0
        row.result_data = data
        db.commit()
    return {"ok": True, "video_id": video_id, "ai_result_id": result_id}


class RoughCutExportBody(BaseModel):
    format: str = "mp4"  # "mp4" | "wav"
    keepRanges: list[dict[str, Any]] = Field(default_factory=list)
    exportSettings: dict[str, Any] = Field(default_factory=dict)
    # Source-clip masks (Task 13). Sanitized server-side in
    # app.services.mask_matte before ever reaching Pillow/ffmpeg.
    masks: list[dict[str, Any]] = Field(default_factory=list)
    # When true, the rendered output registers as the NEXT VERSION of this
    # video (same version_group_id, version = max+1) once the export
    # completes. Back-compat default off — existing callers keep getting
    # only a downloadUrl.
    register_as_version: bool = False


@router.post("/{video_id}/ai/rough-cut-export")
def start_rough_cut_export(
    video_id: int,
    body: RoughCutExportBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_access(video_id, db, current_user)

    fmt = (body.format or "mp4").lower()
    if fmt not in ("mp4", "wav"):
        raise HTTPException(status_code=400, detail="format must be mp4 or wav")

    payload = {
        "format": fmt,
        "keepRanges": body.keepRanges,
        "exportSettings": body.exportSettings,
        "masks": body.masks,
        "progress": 0,
    }

    row = (
        db.query(AiResult)
        .filter(AiResult.video_id == video_id, AiResult.result_type == "rough_cut_export")
        .first()
    )
    if row is None:
        row = AiResult(video_id=video_id, result_type="rough_cut_export")
        db.add(row)

    row.status = "queued"
    row.error_message = None
    row.result_data = payload
    db.commit()
    db.refresh(row)

    from app.jobs.queue import enqueue_rough_cut_export_job

    rq_job_id = enqueue_rough_cut_export_job(row.id, register_as_version=body.register_as_version)
    pdata = dict(row.result_data or {})
    if rq_job_id:
        pdata["rqJobId"] = rq_job_id
        row.result_data = pdata
        db.commit()
    if not rq_job_id:
        row.status = "failed"
        msg = (
            "Background worker unavailable (missing REDIS_URL or enqueue error). "
            "Configure Redis and rq worker for rendered exports."
        )
        row.error_message = msg
        pdata = dict(row.result_data or {})
        pdata["error"] = msg
        pdata["progress"] = 0
        row.result_data = pdata
        db.commit()

    db.refresh(row)
    return {
        "video_id": row.video_id,
        "result_type": row.result_type,
        "status": row.status,
        "result_data": row.result_data,
        "error_message": row.error_message,
        "ai_result_id": row.id,
    }


@router.get("/{video_id}/ai/rough-cut-export")
def get_rough_cut_export(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_access(video_id, db, current_user)
    row = (
        db.query(AiResult)
        .filter(AiResult.video_id == video_id, AiResult.result_type == "rough_cut_export")
        .first()
    )
    if row is None:
        return {
            "video_id": video_id,
            "result_type": "rough_cut_export",
            "status": "idle",
            "result_data": None,
        }
    return {
        "video_id": row.video_id,
        "result_type": row.result_type,
        "status": row.status,
        "result_data": row.result_data,
        "error_message": row.error_message,
        "ai_result_id": row.id,
    }


@router.post("/{video_id}/ai/rough-cut-export/cancel")
def cancel_rough_cut_export(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Best-effort cancel: remove queued RQ job if still pending; mark AiResult cancelled."""
    _check_video_access(video_id, db, current_user)
    row = (
        db.query(AiResult)
        .filter(AiResult.video_id == video_id, AiResult.result_type == "rough_cut_export")
        .first()
    )
    if row is None:
        return {"ok": True, "detail": "no export row"}
    data = dict(row.result_data or {})
    rq_job_id = data.get("rqJobId")
    if isinstance(rq_job_id, str) and rq_job_id.strip():
        try:
            from redis import Redis
            from rq.job import Job

            url = os.environ.get("REDIS_URL", "").strip()
            if url:
                conn = Redis.from_url(url)
                job = Job.fetch(rq_job_id, connection=conn)
                st = job.get_status()
                if st in ("queued", "scheduled", "deferred"):
                    job.cancel()
        except Exception:
            pass
    if row.status in ("queued", "processing"):
        row.status = "failed"
        row.error_message = "Export cancelled."
        pdata = dict(row.result_data or {})
        pdata["progress"] = 0
        pdata["error"] = "Export cancelled."
        row.result_data = pdata
        db.commit()
    return {"ok": True, "video_id": video_id}


class BrollImageRequest(BaseModel):
    transcript_text: str
    start: float
    end: float


@router.post("/{video_id}/ai/generate-broll-image")
def generate_broll_image_endpoint(
    video_id: int,
    body: BrollImageRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_access(video_id, db, current_user)
    try:
        result = generate_broll_image(body.transcript_text)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"AI image generation failed: {exc}") from exc
    try:
        image_url = upload_image_bytes(
            result["image_bytes"],
            mime_type=result["mime_type"],
            folder=f"broll/{video_id}",
            public_id=f"broll_{uuid.uuid4().hex[:8]}",
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Image upload failed: {exc}") from exc
    data = {
        "image_url": image_url,
        "prompt": result["prompt"],
        "keyword": result["keyword"],
        "start": body.start,
        "end": body.end,
    }
    return _upsert_result(db, video_id, "broll_image", data)


@router.get("/{video_id}/ai/results")
def list_ai_results(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_access(video_id, db, current_user)
    rows = db.query(AiResult).filter(AiResult.video_id == video_id).order_by(AiResult.created_at.desc()).all()
    return [
        {
            "video_id": r.video_id,
            "result_type": r.result_type,
            "status": r.status,
            "result_data": r.result_data,
            "error_message": r.error_message,
            "updated_at": r.updated_at,
        }
        for r in rows
    ]
