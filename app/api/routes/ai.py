from __future__ import annotations

import json
import os
import re
import uuid
from copy import deepcopy
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
from app.services.project_access import assert_write_project_content, can_access_project
from app.services import draft_store
from app.services.rough_cut_workspace import (
    WORKSPACE_PERSISTENCE_KEY,
    WORKSPACE_SCHEMA_VERSION,
)
from app.services.job_analytics import record_job_canceled
from app.services.product_analytics import emit
from app.services.editor_feature_analytics import changed_active_editor_features

router = APIRouter(
    prefix="/videos",
    tags=["AI Features"],
)

_ROUGH_CUT_EFFECT_FEATURE_KEYS = {
    "audio": "audio_edit",
    "adjust": "color_adjust",
    "remove_bg": "background_removal",
    "retouch": "retouch",
    "speed": "rough_cut",
}


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
    video = _check_video_access(video_id, db, current_user)
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
    project = db.query(Project).filter(Project.id == video.project_id).first()
    emit(
        db,
        "feature_completed",
        user=current_user,
        workspace_id=project.workspace_id if project else None,
        properties={
            "feature_key": "translation",
            "project_id": video.project_id,
            "video_id": video.id,
            "target_language": target_language,
            "completion_type": "translation_generated",
            "result": "success",
        },
    )
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
                audio_analysis=transcription.audio_analysis
                if isinstance(transcription.audio_analysis, dict)
                else None,
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
    audio_analysis = tr.audio_analysis if tr and isinstance(tr.audio_analysis, dict) else None
    analysis = _analyze_segments(
        segments,
        duration,
        remove_fillers=body.remove_fillers,
        remove_silences=body.remove_silences,
        remove_bad_takes=body.remove_bad_takes,
        remove_repeats=body.remove_repeats,
        aggressiveness=body.aggressiveness,
        vad_silences=audio_analysis.get("silences") if audio_analysis else None,
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
    requested_video = _check_video_access(video_id, db, current_user)
    # Reads never write. The legacy asset-draft merge that used to commit from
    # inside this handler happens in memory in the store and is persisted only
    # by the next save.
    view = draft_store.get_draft_for_video(db, requested_video)
    if view.row is None and not view.payload:
        return {
            "video_id": view.video_id or requested_video.id,
            "result_type": "rough_cut_draft",
            "status": "pending",
            "result_data": None,
            "updated_at": None,
            "revision": 0,
            "checksum": None,
        }
    return {
        "video_id": view.video_id or requested_video.id,
        "result_type": "rough_cut_draft",
        "status": "completed",
        "result_data": view.payload,
        "error_message": None,
        "updated_at": view.row.updated_at if view.row is not None else None,
        "revision": view.revision,
        "checksum": view.checksum,
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
    #: Optimistic concurrency. When supplied, a stale save returns 409 with the
    #: current revision instead of silently clobbering another writer.
    expected_revision: int | None = None


#: Draft keys that hold the actual edit. A PUT that omits one of these keeps
#: the stored value instead of resetting it to the model default — the
#: create-project wizard's 7-key seed body used to wipe the whole timeline
#: this way (plan §5.2 G2).
_DRAFT_STRUCTURAL_KEYS = (
    "keepRanges", "audioKeepRanges", "mutedRanges", "markers", "segments",
    "clipAttributes", "timelineMediaItems", "timelineTracks", "trackState",
    "transitions", "lowerThirds", "elementOverlays", "gridClips",
    "textOverlay", "textOverlays", "transcriptComments", "speakerIdentities",
    "wordHighlights", "wordFormats", "wordTextOverrides", "captionStyle",
)


@router.put("/{video_id}/ai/rough-cut-draft")
def save_rough_cut_draft(
    video_id: int,
    body: RoughCutDraftBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    requested_video = _check_video_access(video_id, db, current_user)
    # Writing a timeline is content mutation. The old read-level check let a
    # guest workspace member PUT the whole draft.
    project = db.query(Project).filter(Project.id == requested_video.project_id).first()
    assert_write_project_content(db, current_user, project)

    view = draft_store.get_draft_for_video(db, requested_video)
    previous_payload = dict(view.payload)

    sent_keys = set(body.model_fields_set) | set((body.model_extra or {}).keys())
    payload = body.model_dump(mode="json", exclude_none=False)
    payload.pop("expected_revision", None)
    # Declared fields the client did not send are not writes: keep the stored
    # value when one exists, and never inject a bare default (`effectJobs: []`
    # used to appear on every save without any client ever sending it).
    for key in list(payload.keys()):
        if key in sent_keys:
            continue
        if key in previous_payload:
            payload[key] = deepcopy(previous_payload[key])
        elif key in _DRAFT_STRUCTURAL_KEYS or key == "effectJobs":
            payload.pop(key, None)
    # Structural keys that are undeclared extras (`timelineMediaItems`,
    # `transitions`, …) are simply absent from the dump when unsent — carry
    # the stored value over so a partial body cannot wipe them either.
    for key in _DRAFT_STRUCTURAL_KEYS:
        if key not in sent_keys and key not in payload and key in previous_payload:
            payload[key] = deepcopy(previous_payload[key])

    metadata = previous_payload.get(WORKSPACE_PERSISTENCE_KEY)
    payload[WORKSPACE_PERSISTENCE_KEY] = (
        dict(metadata)
        if isinstance(metadata, dict)
        else {"schemaVersion": WORKSPACE_SCHEMA_VERSION, "legacyDraftVideoIds": []}
    )

    try:
        saved = draft_store.save_draft(
            db,
            requested_video.project_id,
            payload,
            writer="editor",
            expected_revision=body.expected_revision,
            video_id=view.video_id or requested_video.id,
            user_id=current_user.id,
        )
    except draft_store.DraftConflict as conflict:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "draft_conflict",
                "message": str(conflict),
                "current_revision": conflict.current_revision,
                "checksum": conflict.current_checksum,
            },
        ) from conflict

    for feature_key in changed_active_editor_features(previous_payload, payload):
        emit(
            db,
            "feature_completed",
            user=current_user,
            workspace_id=project.workspace_id if project else None,
            properties={
                "feature_key": feature_key,
                "project_id": requested_video.project_id,
                "video_id": saved.video_id or requested_video.id,
                "completion_type": "draft_saved",
                "result": "success",
            },
        )
    return {
        "video_id": saved.video_id or requested_video.id,
        "result_type": "rough_cut_draft",
        "status": "completed",
        "result_data": saved.payload,
        "revision": saved.revision,
        "checksum": saved.checksum,
    }


class RoughCutEffectBody(BaseModel):
    effect_type: str
    clip_key: str
    clip_target: dict[str, Any] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)


class SegmentPreviewBody(BaseModel):
    """One-frame preview request. `selection` is the editor's stored shape."""

    at_seconds: float = 0.0
    mode: str = "custom"
    selection: dict[str, Any] = Field(default_factory=dict)
    quality: str = "faster"
    #: The clip's `removeBg` attributes, so the preview applies the same invert /
    #: grow / feather / strength the export will. Sent whole rather than as named
    #: fields so adding a control does not require changing this contract.
    refine: dict[str, Any] = Field(default_factory=dict)


class RetouchPreviewBody(BaseModel):
    at_seconds: float = 0.0
    settings: dict[str, Any] = Field(default_factory=dict)


class VisualPreviewBody(BaseModel):
    """Exact paused-frame preview for the visual inspector stack."""

    at_seconds: float = 0.0
    clip_start: float = 0.0
    retouch_settings: dict[str, Any] = Field(default_factory=dict)
    adjust_settings: dict[str, Any] = Field(default_factory=dict)
    # Opening Retouch requests analysis even before a slider is non-zero. This
    # lets the UI gate controls and draw targets instead of asking users to
    # make a blind adjustment first.
    analyze_retouch: bool = False
    # A completed Retouch row is a clip-local source beginning at t=0. Remove
    # BG is deliberately excluded because decoding to BGR would destroy alpha.
    processed_result_id: int | None = None


class RetouchTargetBox(BaseModel):
    x: float
    y: float
    width: float
    height: float


class RetouchPoint(BaseModel):
    x: float
    y: float


class RetouchPartsResponse(BaseModel):
    eyes: bool = False
    nose: bool = False
    mouth: bool = False


class RetouchLandmarksResponse(BaseModel):
    eyes: list[RetouchPoint] = Field(default_factory=list)
    nose: RetouchPoint
    mouth: RetouchPoint


class RetouchDetectionResponse(BaseModel):
    id: str
    kind: str
    box: RetouchTargetBox
    parts: RetouchPartsResponse
    landmarks: RetouchLandmarksResponse


class RetouchCapabilitiesResponse(BaseModel):
    face: bool = False
    skin: bool = False
    eyes: bool = False
    nose: bool = False
    mouth: bool = False
    multipleFaces: bool = False


class RetouchAnalysisResponse(BaseModel):
    width: int
    height: int
    detections: list[RetouchDetectionResponse] = Field(default_factory=list)
    capabilities: RetouchCapabilitiesResponse


class VisualPreviewResponse(BaseModel):
    width: int
    height: int
    image_png: str
    face_count: int | None = None
    retouch_analysis: RetouchAnalysisResponse | None = None


@router.post("/{video_id}/ai/retouch-preview")
def retouch_preview(
    video_id: int,
    body: RetouchPreviewBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Render one exact beauty frame for responsive paused-frame tuning."""
    video = _check_video_access(video_id, db, current_user)
    from app.jobs.rough_cut_effect import _resolve_media_source
    from app.services.retouch import encode_preview_png
    from app.services.segmentation.preview import extract_frame

    try:
        import base64
        import cv2  # type: ignore

        frame_rgb = extract_frame(
            _resolve_media_source(video.file_path),
            max(0.0, float(body.at_seconds or 0.0)),
        )
        frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
        png, width, height, face_count = encode_preview_png(frame_bgr, body.settings)
        return {
            "width": width,
            "height": height,
            "image_png": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
            "face_count": face_count,
        }
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{video_id}/ai/visual-preview", response_model=VisualPreviewResponse)
def visual_preview(
    video_id: int,
    body: VisualPreviewBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Render Retouch then Adjust with the same engines as the final export."""
    video = _check_video_access(video_id, db, current_user)
    from app.jobs.rough_cut_effect import _resolve_media_source
    from app.services.color_adjust import apply_adjust_frame
    from app.services.retouch.beauty import (
        BeautyState,
        analyze_faces,
        beautify_frame,
        serialize_face_analysis,
    )
    from app.services.retouch.settings import has_retouch_adjustments
    from app.services.segmentation.preview import extract_frame

    source = _resolve_media_source(video.file_path)
    at_seconds = max(0.0, float(body.at_seconds or 0.0))
    if body.processed_result_id is not None:
        row = (
            db.query(AiResult)
            .filter(
                AiResult.id == body.processed_result_id,
                AiResult.video_id == video_id,
                AiResult.result_type == "rough_cut_effect",
                AiResult.status == "completed",
            )
            .first()
        )
        data = row.result_data if row is not None and isinstance(row.result_data, dict) else {}
        url = data.get("outputUrl")
        if data.get("effectType") != "retouch" or not isinstance(url, str) or not url.strip():
            raise HTTPException(status_code=422, detail="The processed Retouch preview is no longer available.")
        source = _resolve_media_source(url.strip())
        at_seconds = max(0.0, at_seconds - max(0.0, float(body.clip_start or 0.0)))

    try:
        import base64
        import cv2  # type: ignore

        frame_rgb = extract_frame(source, at_seconds)
        frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
        analysis: dict[str, Any] | None = None
        should_retouch = (
            body.processed_result_id is None
            and has_retouch_adjustments(body.retouch_settings)
        )
        if body.analyze_retouch or should_retouch:
            state = BeautyState()
            detections = analyze_faces(frame_bgr, state, target="all")
            analysis = serialize_face_analysis(frame_bgr, detections)
            if should_retouch:
                frame_bgr = beautify_frame(
                    frame_bgr,
                    body.retouch_settings,
                    state,
                    detections=detections,
                )
        from app.services.lut import resolve_adjust_lut

        adjust_settings = resolve_adjust_lut(
            db, dict(body.adjust_settings or {}), user_id=current_user.id
        )
        frame_bgr = apply_adjust_frame(frame_bgr, adjust_settings)
        ok, encoded = cv2.imencode(".png", frame_bgr, [cv2.IMWRITE_PNG_COMPRESSION, 4])
        if not ok:
            raise RuntimeError("Could not encode the exact visual preview.")
        height, width = frame_bgr.shape[:2]
        return {
            "width": width,
            "height": height,
            "image_png": "data:image/png;base64," + base64.b64encode(encoded.tobytes()).decode("ascii"),
            "face_count": len(analysis["detections"]) if analysis is not None else None,
            "retouch_analysis": analysis,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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
    from app.services.segmentation.preview import (
        preview_auto_mask_png,
        preview_selection_mask_png,
    )

    quality = "better" if str(body.quality).strip().lower() == "better" else "faster"
    mode = "auto" if str(body.mode).strip().lower() == "auto" else "custom"

    try:
        source = _resolve_media_source(video.file_path)
        if mode == "auto":
            png, width, height = preview_auto_mask_png(
                source,
                float(body.at_seconds or 0.0),
                quality=quality,
                settings=body.refine,
            )
            point_count = 0
        else:
            prompts = LocalSegmentationProvider._selection_prompts({"selection": body.selection})
            if prompts is None:
                raise HTTPException(status_code=400, detail="Click the subject to select it first.")
            png, width, height = preview_selection_mask_png(
                source,
                float(body.at_seconds or 0.0),
                prompts,
                quality=quality,
                # Invert / grow / feather come from the same stored attributes the
                # export reads, so the preview shows the refined matte, not a raw one.
                settings=body.refine,
            )
            point_count = prompts.point_count
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
        "point_count": point_count,
    }


@router.get("/{video_id}/ai/segmentation-capabilities")
def segmentation_capabilities(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Reports what this deployment can actually offer before a user clicks it."""
    _check_video_access(video_id, db, current_user)
    from app.services.segmentation import get_provider
    from app.services.segmentation.base import (
        CAPABILITY_AUTO_MATTE,
        CAPABILITY_POINT_PROMPT,
        CAPABILITY_PROPAGATE,
    )

    provider = get_provider()
    ready, reason = provider.is_available()
    return {
        "provider": provider.name,
        "ready": ready,
        "reason": reason or None,
        "auto": provider.supports(CAPABILITY_AUTO_MATTE),
        "custom": provider.supports(CAPABILITY_POINT_PROMPT),
        "propagate": provider.supports(CAPABILITY_PROPAGATE),
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
    video = _check_video_access(video_id, db, current_user)
    project = db.query(Project).filter(Project.id == video.project_id).first()
    row = (
        db.query(AiResult)
        .filter(AiResult.id == result_id, AiResult.video_id == video_id, AiResult.result_type == "rough_cut_effect")
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Effect job not found")
    data = dict(row.result_data or {})
    rq_job_id = data.get("rqJobId")
    stopped = False

    if isinstance(rq_job_id, str) and rq_job_id.strip():
        try:
            from redis import Redis
            from rq.command import send_stop_job_command
            from rq.job import Job

            url = os.environ.get("REDIS_URL", "").strip()
            if url:
                connection = Redis.from_url(url)
                job = Job.fetch(rq_job_id, connection=connection)
                status = job.get_status(refresh=True)
                if status in ("queued", "scheduled", "deferred"):
                    job.cancel()
                    stopped = True
                elif status == "started":
                    # `job.cancel()` only removes a job from the queue — it does
                    # nothing to one already running, so cancelling a started job
                    # used to mark the row failed while the work-horse carried on
                    # burning GPU. This actually stops it.
                    send_stop_job_command(connection, rq_job_id)
                    stopped = True
        except Exception:
            # Redis unreachable, or the job already finished between the fetch and
            # the stop. The row is still marked below — a cancel the user asked
            # for should not appear to have been ignored.
            pass

    if row.status in ("queued", "processing"):
        # "canceled", not "failed". The user did this on purpose, and rendering it
        # as an error puts a red alarm on an intentional action.
        row.status = "canceled"
        row.error_message = None
        data["status"] = "canceled"
        data["progress"] = 0
        data.pop("error", None)
        data.pop("errorDetail", None)
        # Read by the job itself before it writes a result, so work that finishes
        # in the gap cannot resurrect a cancelled row.
        data["canceled"] = True
        row.result_data = data
        record_job_canceled(
            db,
            job_kind="rough_cut_effect",
            job_id=row.id,
            feature_key=_ROUGH_CUT_EFFECT_FEATURE_KEYS.get(
                str(data.get("effectType") or "").strip(), "rough_cut"
            ),
            user=current_user,
            project=project,
        )
        db.commit()

        clip_key = str(data.get("clipKey") or "").strip()
        effect_type = str(data.get("effectType") or "").strip()
        if clip_key and effect_type:
            from app.jobs.rough_cut_effect import _attach_to_draft

            _attach_to_draft(
                db,
                row.video_id,
                clip_key,
                effect_type,
                {"resultId": row.id, "status": "canceled", "progress": 0},
            )

    return {
        "ok": True,
        "video_id": video_id,
        "ai_result_id": result_id,
        # Whether the running work was actually signalled, as opposed to only the
        # row being marked. Useful when a stop could not be delivered.
        "stopped": stopped,
        "status": row.status,
    }


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
    video = _check_video_access(video_id, db, current_user)
    project = db.query(Project).filter(Project.id == video.project_id).first()
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
        row.status = "canceled"
        row.error_message = None
        data["status"] = "canceled"
        data.pop("error", None)
        data["progress"] = 0
        row.result_data = data
        record_job_canceled(
            db,
            job_kind="mask_track",
            job_id=row.id,
            feature_key="mask_tracking",
            user=current_user,
            project=project,
        )
        db.commit()
    return {"ok": True, "video_id": video_id, "ai_result_id": result_id}


# Rasterized overlay frames the browser sends as base64 PNGs. There is no
# global body-size limit in this app, so the count and per-frame size are
# bounded here, before the payload is persisted onto the AiResult row.
_MAX_BURN_INS = 32
_MAX_BURN_IN_PNG_BYTES = 6 * 1024 * 1024


class RoughCutExportBody(BaseModel):
    format: str = "mp4"  # "mp4" | "wav"
    keepRanges: list[dict[str, Any]] = Field(default_factory=list)
    exportSettings: dict[str, Any] = Field(default_factory=dict)
    # Source-clip masks (Task 13). Sanitized server-side in
    # app.services.mask_matte before ever reaching Pillow/ffmpeg.
    masks: list[dict[str, Any]] = Field(default_factory=list)
    # Completed Remove BG / Retouch visual renders keyed to their source range.
    # The job verifies every URL against this video's effect rows before use.
    processedRanges: list[dict[str, Any]] = Field(default_factory=list)
    # Non-destructive per-source-range color settings. The worker validates and
    # translates these into a deterministic ffmpeg filter chain.
    colorRanges: list[dict[str, Any]] = Field(default_factory=list)
    # Non-destructive Canvas and temporal-motion settings per source range.
    videoRanges: list[dict[str, Any]] = Field(default_factory=list)
    # Per-clip A-roll audio aligned with keepRanges by exact source range:
    # {start, end, volume?(dB), fadeIn?(sec), fadeOut?(sec)}. Flat on purpose --
    # the worker sanitizes every number before it reaches an ffmpeg filter.
    audioRanges: list[dict[str, Any]] = Field(default_factory=list)
    # Source spans the editor silenced entirely: {start, end} in source seconds.
    mutedRanges: list[dict[str, Any]] = Field(default_factory=list)
    # First-class clips placed on tracks above the source: picture
    # (kind "video"/"image") and audio lanes (kind "audio" -- music/SFX, no
    # picture, mixed rather than composited, audible unless `audioEnabled` is
    # explicitly false). The browser sends ids/timing/settings only; the worker
    # resolves owned media and completed effect outputs instead of trusting
    # client URLs.
    timelineLayers: list[dict[str, Any]] = Field(default_factory=list)
    # Cut/edge transitions. Preset names are resolved and allow-listed again
    # in the worker; clip indices are advisory and source ranges are canonical.
    transitions: list[dict[str, Any]] = Field(default_factory=list)
    # Pre-rasterized full-frame transparent PNG overlays (text, lower thirds,
    # brand) with their output-timeline spans:
    # {png (base64, no data: prefix), start, end, fadeIn?, fadeOut?}.
    # The browser has already positioned and alpha-composited each frame; the
    # worker only decodes, validates and overlays it at 0:0.
    burnIns: list[dict[str, Any]] = Field(default_factory=list)
    burnInsIncludeLowerThirds: bool = False
    # When true, the rendered output registers as the NEXT VERSION of this
    # video (same version_group_id, version = max+1) once the export
    # completes. Back-compat default off — existing callers keep getting
    # only a downloadUrl.
    register_as_version: bool = False
    #: Fail closed on matte failure: a redaction or harness composite whose
    #: unmasked form is wrong must stop the export, not ship without the mask.
    masksRequired: bool = False
    #: The editor's caption look (plus a client-resolved `fontFamily`). When
    #: present, burned captions render as styled ASS instead of a bare SRT at
    #: libass defaults — closing the biggest preview/export gap (plan §5.3).
    captionStyle: dict[str, Any] = Field(default_factory=dict)


@router.post("/{video_id}/ai/rough-cut-export")
def start_rough_cut_export(
    video_id: int,
    body: RoughCutExportBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    video = _check_video_access(video_id, db, current_user)
    project = db.query(Project).filter(Project.id == video.project_id).first()

    fmt = (body.format or "mp4").lower()
    if fmt not in ("mp4", "wav"):
        raise HTTPException(status_code=400, detail="format must be mp4 or wav")

    # Bound the burn-in frames before they are written to the AiResult row.
    # Oversize is measured on the encoded string -- base64 carries 3 bytes per
    # 4 characters -- so a 50 MB blob is rejected without ever being decoded.
    # The worker re-validates everything (magic bytes, spans); this is only
    # about not storing megabytes the render would throw away.
    burn_ins: list[dict[str, Any]] = []
    burn_ins_rejected = 0
    for item in body.burnIns:
        if len(burn_ins) >= _MAX_BURN_INS:
            burn_ins_rejected += 1
            continue
        if not isinstance(item, dict):
            burn_ins_rejected += 1
            continue
        png = item.get("png")
        if not isinstance(png, str) or not png.strip():
            burn_ins_rejected += 1
            continue
        if (len(png.strip()) * 3) // 4 > _MAX_BURN_IN_PNG_BYTES:
            burn_ins_rejected += 1
            continue
        burn_ins.append(item)

    payload = {
        "format": fmt,
        "keepRanges": body.keepRanges,
        "exportSettings": body.exportSettings,
        "masks": body.masks,
        "masksRequired": body.masksRequired,
        "captionStyle": body.captionStyle,
        "processedRanges": body.processedRanges,
        "colorRanges": body.colorRanges,
        "videoRanges": body.videoRanges,
        "audioRanges": body.audioRanges,
        "mutedRanges": body.mutedRanges,
        "timelineLayers": body.timelineLayers[:64],
        "transitions": body.transitions[:128],
        "burnIns": burn_ins,
        "burnInsRejected": burn_ins_rejected,
        "burnInsIncludeLowerThirds": body.burnInsIncludeLowerThirds,
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
        emit(
            db,
            "feature_started",
            event_id=f"rough-cut-export:{row.id}:feature-started",
            user=current_user,
            workspace_id=project.workspace_id if project else None,
            properties={
                "feature_key": "export",
                "project_id": video.project_id,
                "video_id": video.id,
                "export_format": fmt,
                "entry_point": "rough_cut",
            },
        )
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
        emit(
            db,
            "job_failed",
            event_id=f"rough-cut-export:{row.id}:queue-failed",
            user=current_user,
            workspace_id=project.workspace_id if project else None,
            properties={
                "job_id": f"export:{row.id}",
                "job_type": "rough_cut_export_job",
                "feature_key": "export",
                "project_id": video.project_id,
                "video_id": video.id,
                "error_code": "queue_unavailable",
                "failure_class": "queue",
                "result": "failure",
            },
        )
        emit(
            db,
            "feature_failed",
            event_id=f"rough-cut-export:{row.id}:feature-queue-failed",
            user=current_user,
            workspace_id=project.workspace_id if project else None,
            properties={
                "feature_key": "export",
                "project_id": video.project_id,
                "video_id": video.id,
                "error_code": "queue_unavailable",
                "failure_class": "queue",
                "result": "failure",
            },
        )
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
    video = _check_video_access(video_id, db, current_user)
    project = db.query(Project).filter(Project.id == video.project_id).first()
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
        row.status = "canceled"
        row.error_message = None
        pdata = dict(row.result_data or {})
        pdata["progress"] = 0
        pdata["status"] = "canceled"
        pdata.pop("error", None)
        row.result_data = pdata
        record_job_canceled(
            db,
            job_kind="rough_cut_export",
            job_id=row.id,
            feature_key="export",
            user=current_user,
            project=project,
        )
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
