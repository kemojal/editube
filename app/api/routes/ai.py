from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import AiResult, Comment, Project, Video, VideoTranscription, User
from app.jobs.ai_jobs import (
    build_briefing_digest,
    build_video_metadata,
    detect_chapters,
    detect_fillers,
)
from app.services.ai_client import generate_json
from app.utils.security import get_current_user

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
    if current_user not in [project.creator] + [c.user for c in project.collaborators]:
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


@router.post("/{video_id}/ai/rough-cut")
def rough_cut(
    video_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_access(video_id, db, current_user)
    return _upsert_result(
        db,
        video_id,
        "rough_cut",
        {"status": "queued", "brief": body.get("brief", ""), "clip_ids": body.get("clip_ids", [])},
        status="queued",
    )


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
