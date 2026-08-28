"""
RQ job: LLM chapter suggestions from transcript -> VideoChapter rows (source=llm).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.db.models import Video, VideoChapter, VideoTranscription
from app.services.ai_client import generate_json

logger = logging.getLogger(__name__)


def chapter_synthesis_job(video_id: int) -> None:
    db: Session = SessionLocal()
    try:
        video = db.query(Video).filter(Video.id == video_id).first()
        if not video:
            raise RuntimeError(f"Video {video_id} was removed before chapter synthesis started")

        tr = db.query(VideoTranscription).filter(VideoTranscription.video_id == video_id).first()
        segments = tr.segments if tr and tr.segments else []
        if not segments:
            raise RuntimeError(f"Video {video_id} has no transcript for chapter synthesis")

        transcript = " ".join(str(seg.get("text", "")).strip() for seg in segments if isinstance(seg, dict))
        if not transcript.strip():
            raise RuntimeError(f"Video {video_id} has an empty transcript")

        fallback: dict[str, Any] = {"chapters": []}
        prompt = (
            "From this transcript, propose YouTube-style chapter markers. "
            "Return JSON only: {\"chapters\": [{\"start_sec\": number, \"title\": string}]}. "
            "Rules: 4–20 chapters, start_sec non-decreasing, at least 30 seconds apart, "
            "first chapter at 0, titles under 80 chars, no HTML.\n\n"
            f"Transcript:\n{transcript[:14000]}"
        )
        result = generate_json(prompt, fallback=fallback)
        raw_chapters = result.get("chapters") if isinstance(result, dict) else None
        if not isinstance(raw_chapters, list):
            raise RuntimeError("Chapter synthesis returned an invalid response")

        db.query(VideoChapter).filter(
            VideoChapter.video_id == video_id,
            VideoChapter.source == "llm",
        ).delete(synchronize_session=False)

        order = 0
        last_start = -999
        for item in raw_chapters:
            if not isinstance(item, dict):
                continue
            try:
                st = int(float(item.get("start_sec", item.get("start", 0))))
            except (TypeError, ValueError):
                continue
            title = str(item.get("title", "")).strip()[:200]
            if not title:
                continue
            if order > 0 and st < last_start + 30:
                continue
            last_start = st
            db.add(
                VideoChapter(
                    video_id=video_id,
                    start_time=st,
                    end_time=None,
                    title=title,
                    source="llm",
                    order_index=order,
                )
            )
            order += 1
            if order >= 24:
                break

        db.commit()
        logger.info("chapter_synthesis_job: wrote %s LLM chapters for video %s", order, video_id)

    except Exception:
        logger.exception("chapter_synthesis_job failed for video %s", video_id)
        try:
            db.rollback()
        except Exception:
            pass
        raise
    finally:
        db.close()
