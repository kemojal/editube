"""Shared "create a project Video from a YouTube URL" flow.

Used by both:
- POST /repurpose/jobs (source_mode="youtube_url") in app/api/routes/clips.py
- POST /projects/{project_id}/videos/youtube in app/api/routes/videos.py

so the resolve-stream / fetch-metadata / create-Video / seed-transcription logic
lives in exactly one place instead of being duplicated across call sites.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.db.models import User, Video, VideoTranscription
from app.services.transcription_enqueue import prepare_and_enqueue_transcription
from app.services.youtube_stream_resolve import (
    YoutubeStreamResolveError,
    fetch_youtube_page_metadata,
    resolve_youtube_page_to_stream_url,
    youtube_thumbnail_url,
)
from app.utils.language import normalize_language

logger = logging.getLogger(__name__)


def create_youtube_source_video(
    db: Session,
    *,
    user: User,
    project_id: int,
    youtube_url: str,
    name: Optional[str] = None,
    language: Optional[str] = None,
    thumbnail_url: Optional[str] = None,
    duration_seconds: Optional[int] = None,
    enqueue_transcription: bool = True,
) -> Video:
    """
    Resolve `youtube_url` to a direct media stream (yt-dlp), create the Video row
    (file_path=stream URL, ingest_page_url=canonical watch URL), and seed a pending
    transcription for it.

    Title/thumbnail/duration are fetched via yt-dlp+oEmbed metadata only when the
    caller hasn't already supplied them (callers that already resolved metadata,
    e.g. via /repurpose/youtube-metadata, can pass it through to avoid a second
    yt-dlp round-trip). `name` defaults to the fetched YouTube title, else
    "YouTube source".

    When `enqueue_transcription` is True (default), a pending VideoTranscription
    row is created and transcription is enqueued immediately via
    `prepare_and_enqueue_transcription` (with `language`). When False, only the
    pending row is seeded — the caller (e.g. the repurpose job pipeline) is
    responsible for enqueuing on its own schedule.

    Raises YoutubeStreamResolveError if yt-dlp cannot resolve a playable stream.
    """
    canonical_url = (youtube_url or "").strip()
    stream_url = resolve_youtube_page_to_stream_url(canonical_url)

    title: Optional[str] = None
    if not name or thumbnail_url is None or duration_seconds is None:
        try:
            media = fetch_youtube_page_metadata(canonical_url)
        except YoutubeStreamResolveError:
            # Stream resolve already succeeded; a metadata fetch failure just
            # means we degrade gracefully to no title/thumbnail/duration.
            media = {}
        title = media.get("title")
        if thumbnail_url is None:
            thumbnail_url = media.get("thumbnail")
        if duration_seconds is None:
            raw_duration = media.get("duration")
            try:
                duration_seconds = int(float(raw_duration)) if raw_duration else None
            except (TypeError, ValueError):
                duration_seconds = None

    thumbnail_url = thumbnail_url or youtube_thumbnail_url(canonical_url)
    resolved_name = (name or title or "YouTube source").strip()[:255]

    latest_version = (
        db.query(Video)
        .filter(Video.project_id == project_id)
        .order_by(Video.version.desc())
        .first()
    )
    version = 1 if latest_version is None else (latest_version.version or 0) + 1

    video = Video(
        project_id=project_id,
        name=resolved_name,
        version=version,
        file_path=stream_url,
        ingest_page_url=canonical_url,
        thumbnail_url=thumbnail_url,
        duration=duration_seconds,
        uploader_id=user.id,
        status="in_progress",
    )
    db.add(video)
    db.flush()

    if enqueue_transcription:
        prepare_and_enqueue_transcription(db, video.id, language=language)
    else:
        vt = db.query(VideoTranscription).filter(VideoTranscription.video_id == video.id).first()
        if vt is None:
            db.add(
                VideoTranscription(
                    video_id=video.id,
                    status="pending",
                    language=normalize_language(language),
                )
            )
        db.commit()

    return video
