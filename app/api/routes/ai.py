from __future__ import annotations

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
from app.services.ai_client import generate_broll_image, generate_json
from app.services.auto_edit import (
    AutoEditOptions,
    _analyze_segments,
    _summarize_analysis,
)
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


@router.post("/{video_id}/ai/review")
def review_video(
    video_id: int,
    body: AutoEditOptions | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Holistic AI review: engagement score + filler/silence/bad-take counts + suggestions."""
    video = _check_video_access(video_id, db, current_user)
    tr = db.query(VideoTranscription).filter(VideoTranscription.video_id == video_id).first()
    segments = list(tr.segments) if tr and tr.segments else []
    duration = float(getattr(video, "duration", 0) or 0)
    opts = body or AutoEditOptions()

    if not segments:
        return _upsert_result(
            db,
            video_id,
            "review",
            {
                "needs_transcription": True,
                "engagement_score": None,
                "verdict": "Transcribe the video first to run an AI review.",
                "hook": "",
                "pacing": "",
                "strengths": [],
                "improvements": [],
                "counts": {"fillers": 0, "silences": 0, "bad_takes": 0},
                "removable_seconds": 0,
                "keepRanges": [],
            },
        )

    analysis = _analyze_segments(
        segments,
        duration,
        remove_fillers=opts.remove_fillers,
        remove_silences=opts.remove_silences,
        remove_bad_takes=opts.remove_bad_takes,
        aggressiveness=opts.aggressiveness,
    )
    counts, removable = _summarize_analysis(analysis)

    comments = (
        db.query(Comment)
        .filter(Comment.video_id == video_id)
        .order_by(Comment.timecode.asc())
        .limit(60)
        .all()
    )

    # Heuristic engagement score — also the LLM fallback when GEMINI_API_KEY is unset.
    minutes = max(duration / 60.0, 0.5)
    filler_rate = counts["fillers"] / minutes
    penalty = (
        min(filler_rate * 4, 30)
        + min(counts["silences"] * 2, 20)
        + min(counts["bad_takes"] * 5, 25)
    )
    heuristic_score = int(max(35, round(100 - penalty)))

    fallback = {
        "engagement_score": heuristic_score,
        "verdict": "Heuristic estimate (set GEMINI_API_KEY for a full AI review).",
        "hook": "",
        "pacing": "",
        "strengths": [],
        "improvements": [],
    }

    context = {
        "transcript": segments[:400],
        "comments": [{"timecode": c.timecode, "text": c.text} for c in comments],
        "stats": {"duration_sec": duration, **counts},
    }
    review = generate_json(
        "You are an expert video editor reviewing a creator's video from its "
        "transcript. Rate audience engagement 0-100 (hook strength, pacing, "
        "clarity, retention risk). Be specific and reference timecodes.\n"
        "Return JSON: {engagement_score:number, verdict:string, hook:string, "
        'pacing:string, strengths:string[], improvements:[{text:string, '
        'start:number, severity:"low"|"medium"|"high"}]}\n'
        f"{context}",
        fallback=fallback,
    )
    if not isinstance(review, dict):
        review = dict(fallback)

    # Harden the LLM output into a stable shape.
    try:
        score = int(round(float(review.get("engagement_score", heuristic_score))))
    except (TypeError, ValueError):
        score = heuristic_score
    review["engagement_score"] = max(0, min(100, score))
    review["verdict"] = str(review.get("verdict") or "")
    review["hook"] = str(review.get("hook") or "")
    review["pacing"] = str(review.get("pacing") or "")
    review["strengths"] = [str(x) for x in (review.get("strengths") or []) if str(x).strip()][:8]

    improvements = []
    for it in (review.get("improvements") or [])[:12]:
        if isinstance(it, dict):
            text = str(it.get("text") or "").strip()
            if not text:
                continue
            start = it.get("start")
            try:
                start = float(start) if start is not None else None
            except (TypeError, ValueError):
                start = None
            improvements.append(
                {"text": text, "start": start, "severity": it.get("severity") or "medium"}
            )
        elif isinstance(it, str) and it.strip():
            improvements.append({"text": it.strip(), "start": None, "severity": "medium"})
    review["improvements"] = improvements

    payload = {
        **review,
        "needs_transcription": False,
        "counts": counts,
        "removable_seconds": round(removable, 1),
        "keepRanges": analysis.get("keepRanges", []),
    }
    return _upsert_result(db, video_id, "review", payload)


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
    return {
        "video_id": row.video_id,
        "result_type": row.result_type,
        "status": row.status,
        "result_data": row.result_data,
        "error_message": row.error_message,
        "ai_result_id": row.id,
    }


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
