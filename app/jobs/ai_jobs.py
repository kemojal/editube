from __future__ import annotations

from collections import Counter
import re
from typing import Any

from app.db.database import SessionLocal
from app.db.models import AiResult, Comment, VideoTranscription
from app.services.ai_client import generate_json


def _upsert_result(video_id: int, result_type: str, payload: dict[str, Any]) -> None:
    db = SessionLocal()
    try:
        row = (
            db.query(AiResult)
            .filter(AiResult.video_id == video_id, AiResult.result_type == result_type)
            .first()
        )
        if row is None:
            row = AiResult(video_id=video_id, result_type=result_type, status="completed")
            db.add(row)
        row.status = "completed"
        row.error_message = None
        row.result_data = payload
        db.commit()
    finally:
        db.close()


def build_briefing_digest(video_id: int) -> dict[str, Any]:
    db = SessionLocal()
    try:
        comments = (
            db.query(Comment)
            .filter(Comment.video_id == video_id, Comment.parent_id.is_(None))
            .order_by(Comment.timecode.asc())
            .all()
        )
        entries = [
            {
                "timecode": c.timecode,
                "text": c.text,
                "is_resolved": c.is_resolved,
            }
            for c in comments
        ]
    finally:
        db.close()

    fallback = {
        "summary": "No comments available for this video yet.",
        "themes": [],
        "unresolved_items": [],
    }
    if not entries:
        _upsert_result(video_id, "briefing_digest", fallback)
        return fallback

    prompt = (
        "Summarize these timestamped client review comments.\n"
        "Return JSON with: summary (string), themes (array of {title, notes, timecodes}), "
        "unresolved_items (array of {timecode, text}).\n\n"
        f"Comments: {entries}"
    )
    result = generate_json(prompt, fallback=fallback)
    _upsert_result(video_id, "briefing_digest", result)
    return result


def build_video_metadata(video_id: int) -> dict[str, Any]:
    db = SessionLocal()
    try:
        tr = db.query(VideoTranscription).filter(VideoTranscription.video_id == video_id).first()
        segments = tr.segments if tr and tr.segments else []
    finally:
        db.close()

    transcript = " ".join(str(seg.get("text", "")).strip() for seg in segments if isinstance(seg, dict))
    fallback = {
        "title": "Untitled Video",
        "description": transcript[:300] if transcript else "",
        "tags": [],
        "hashtags": [],
    }
    if not transcript:
        _upsert_result(video_id, "metadata", fallback)
        return fallback

    prompt = (
        "Generate YouTube SEO metadata from this transcript.\n"
        "Return JSON only: title (string), description (string), tags (array), hashtags (array).\n\n"
        f"Transcript:\n{transcript[:12000]}"
    )
    result = generate_json(prompt, fallback=fallback)
    _upsert_result(video_id, "metadata", result)
    return result


def detect_fillers(video_id: int) -> dict[str, Any]:
    db = SessionLocal()
    try:
        tr = db.query(VideoTranscription).filter(VideoTranscription.video_id == video_id).first()
        segments = tr.segments if tr and tr.segments else []
    finally:
        db.close()

    filler_words = {"um", "uh", "like", "you know", "sort of", "kind of"}
    fillers = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        text = str(seg.get("text", "")).lower()
        for phrase in filler_words:
            if phrase in text:
                fillers.append(
                    {
                        "start": seg.get("start", 0),
                        "end": seg.get("end", seg.get("start", 0)),
                        "word": phrase,
                    }
                )
    result = {"silences": [], "fillers": fillers, "total_time_saved": 0}
    _upsert_result(video_id, "fillers", result)
    return result


def detect_chapters(video_id: int) -> dict[str, Any]:
    db = SessionLocal()
    try:
        tr = db.query(VideoTranscription).filter(VideoTranscription.video_id == video_id).first()
        segments = tr.segments if tr and tr.segments else []
    finally:
        db.close()

    chapters = []
    chunk_size = 8
    for i in range(0, len(segments), chunk_size):
        chunk = segments[i : i + chunk_size]
        if not chunk:
            continue
        start = int(chunk[0].get("start", 0))
        end = int(chunk[-1].get("end", start))
        text = " ".join(str(s.get("text", "")) for s in chunk)
        words = re.findall(r"[a-zA-Z]{4,}", text.lower())
        top = Counter(words).most_common(2)
        title = " / ".join(w for w, _ in top) if top else f"Chapter {len(chapters)+1}"
        chapters.append(
            {"start": start, "end": end, "title": title.title(), "description": text[:140]}
        )
    result = {"chapters": chapters}
    _upsert_result(video_id, "chapters", result)
    return result
