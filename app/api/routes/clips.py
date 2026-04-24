"""
Repurpose / short-clip API.

Shape mirrors reelcut's /clips domain but adapted to editube conventions:
- Integer IDs, Bearer-JWT auth, snake_case JSON.
- Source videos live in the existing videos table.
- Clips inherit access control from the source video's project.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import re
from typing import List, Optional
import json
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload

from app.db.database import get_db
from app.db.models import (
    Clip,
    ClipStyle,
    ClipTemplate,
    RepurposeJob,
    RepurposeUserDefaults,
    Project,
    User,
    Video,
    VideoTranscription,
)
from app.api.models.clips import (
    ClipCreate,
    ClipOut,
    ClipRenderRequest,
    ClipRenderResponse,
    ClipStyleOut,
    ClipStyleUpdate,
    ClipUpdate,
    SuggestClipsRequest,
    SuggestClipsResponse,
    TemplateCreate,
    TemplateOut,
    TemplateUpdate,
    YoutubeMetadataOut,
    YoutubeMetadataRequest,
    RepurposeJobCreate,
    RepurposeJobOut,
    RepurposeUserDefaultsOut,
    RepurposeUserDefaultsUpdate,
)
from app.services.clip_analysis import suggest_clips as run_suggest_clips
from app.services.clip_captions import (
    blocks_from_cuts,
    to_srt,
    to_vtt,
)
from app.services.clip_cuts import (
    cuts_bounds,
    cuts_total_duration,
    normalize_cuts,
)
from app.services.project_access import can_access_project
from app.services.project_access import assert_write_project_content
from app.services.repurpose_pipeline import start_repurpose_processing
from app.services.youtube_stream_resolve import (
    YoutubeStreamResolveError,
    fetch_youtube_page_metadata,
    resolve_youtube_page_to_stream_url,
)
from app.utils.security import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Repurpose"])


# --- Helpers -------------------------------------------------------------


def _video_with_access(video_id: int, db: Session, user: User) -> Video:
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    project = db.query(Project).filter(Project.id == video.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_access_project(db, user.id, project):
        raise HTTPException(status_code=403, detail="Not authorized to access this video")
    return video


def _project_with_write_access(project_id: int, db: Session, user: User) -> Project:
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_access_project(db, user.id, project):
        raise HTTPException(status_code=403, detail="Not authorized to access this project")
    assert_write_project_content(db, user, project)
    return project


def _clip_with_access(clip_id: int, db: Session, user: User) -> Clip:
    clip = (
        db.query(Clip)
        .options(joinedload(Clip.style))
        .filter(Clip.id == clip_id)
        .first()
    )
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    _video_with_access(clip.video_id, db, user)
    return clip


def _ensure_style(db: Session, clip: Clip) -> ClipStyle:
    if clip.style is not None:
        return clip.style
    style = ClipStyle(clip_id=clip.id)
    db.add(style)
    db.commit()
    db.refresh(style)
    clip.style = style
    return style


def _serialize_clip(clip: Clip) -> dict:
    return ClipOut.model_validate(clip).model_dump()


def _clip_transcript_author(user: User) -> dict:
    return {
        "user_id": user.id,
        "name": (
            getattr(user, "full_name", None)
            or getattr(user, "name", None)
            or getattr(user, "email", None)
            or f"User {user.id}"
        )[:120],
        "avatar_url": getattr(user, "avatar_url", None),
    }


def _normalize_clip_transcript_author(raw: object, fallback: dict, current_user_id: int) -> dict:
    if not isinstance(raw, dict):
        return fallback
    raw_user_id = raw.get("user_id")
    try:
        user_id = int(raw_user_id) if raw_user_id is not None else None
    except (TypeError, ValueError):
        user_id = None
    if user_id == current_user_id:
        return fallback
    name = str(raw.get("name") or "").strip()[:120]
    return {
        "user_id": user_id,
        "name": name or fallback["name"],
        "avatar_url": str(raw.get("avatar_url")).strip() if raw.get("avatar_url") else None,
    }


def _normalize_clip_transcript_highlights(rows: object, current_user: User) -> list[dict]:
    author_fallback = _clip_transcript_author(current_user)
    now = datetime.now(timezone.utc).isoformat()
    normalized: list[dict] = []
    for row in rows or []:
        payload = row.model_dump(mode="json") if hasattr(row, "model_dump") else dict(row or {})
        try:
            start = float(payload.get("start"))
            end = float(payload.get("end"))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        normalized.append(
            {
                "id": str(payload.get("id") or uuid4()),
                "start": start,
                "end": end,
                "color": str(payload.get("color") or "yellow").strip().lower()[:24] or "yellow",
                "start_segment_index": payload.get("start_segment_index"),
                "start_word_index": payload.get("start_word_index"),
                "end_segment_index": payload.get("end_segment_index"),
                "end_word_index": payload.get("end_word_index"),
                "anchor_text": str(payload.get("anchor_text") or "").strip()[:500] or None,
                "author": _normalize_clip_transcript_author(
                    payload.get("author"), author_fallback, current_user.id
                ),
                "created_at": payload.get("created_at") or now,
                "updated_at": payload.get("updated_at") or now,
            }
        )
    normalized.sort(key=lambda item: (item["start"], item["end"], item["created_at"], item["id"]))
    return normalized


def _normalize_clip_transcript_comments(rows: object, current_user: User) -> list[dict]:
    author_fallback = _clip_transcript_author(current_user)
    now = datetime.now(timezone.utc).isoformat()
    normalized: list[dict] = []
    for row in rows or []:
        payload = row.model_dump(mode="json") if hasattr(row, "model_dump") else dict(row or {})
        try:
            start = float(payload.get("start"))
            end = float(payload.get("end"))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        text = str(payload.get("text") or "").strip()
        if not text:
            continue
        normalized.append(
            {
                "id": str(payload.get("id") or uuid4()),
                "start": start,
                "end": end,
                "text": text[:4000],
                "start_segment_index": payload.get("start_segment_index"),
                "start_word_index": payload.get("start_word_index"),
                "end_segment_index": payload.get("end_segment_index"),
                "end_word_index": payload.get("end_word_index"),
                "anchor_text": str(payload.get("anchor_text") or "").strip()[:500] or None,
                "author": _normalize_clip_transcript_author(
                    payload.get("author"), author_fallback, current_user.id
                ),
                "created_at": payload.get("created_at") or now,
                "updated_at": payload.get("updated_at") or now,
            }
        )
    normalized.sort(key=lambda item: (item["start"], item["end"], item["created_at"], item["id"]))
    return normalized


def _extract_youtube_video_id(url: str) -> str | None:
    raw = (url or "").strip()
    if not raw:
        return None

    # Mirror reelcut's URL normalization behavior:
    # users often paste URLs without protocol or with www/mobile host variants.
    if not re.match(r"^https?://", raw, flags=re.IGNORECASE):
        raw = f"https://{raw}"

    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]

    if host == "youtu.be" or host.endswith(".youtu.be"):
        candidate = parsed.path.strip("/").split("/")[0]
        return candidate or None

    if host == "youtube.com" or host.endswith(".youtube.com"):
        if parsed.path == "/watch":
            return parse_qs(parsed.query).get("v", [None])[0]

        if (
            parsed.path.startswith("/shorts/")
            or parsed.path.startswith("/embed/")
            or parsed.path.startswith("/live/")
        ):
            parts = [p for p in parsed.path.split("/") if p]
            return parts[1] if len(parts) > 1 else None

    # Last-resort extraction so pasted snippets like "...watch?v=<id>&..." still work.
    m = re.search(r"(?:v=|/shorts/|/embed/|/live/|youtu\.be/)([A-Za-z0-9_-]{6,})", raw)
    if m:
        return m.group(1)

    return None


def _serialize_job(job: RepurposeJob) -> RepurposeJobOut:
    source_meta = job.source_meta if isinstance(job.source_meta, dict) else {}
    range_start = source_meta.get("source_range_start_seconds")
    range_end = source_meta.get("source_range_end_seconds")
    return RepurposeJobOut(
        id=job.id,
        user_id=job.user_id,
        project_id=job.project_id,
        video_id=job.video_id,
        source_mode=job.source_mode,
        source_url=job.source_url,
        source_file_url=job.source_file_url,
        source_title=job.source_title,
        source_meta=job.source_meta,
        clip_mode=job.clip_mode,
        clip_anything_prompt=job.clip_anything_prompt,
        genres=list(job.genres or []),
        clip_length_bucket=job.clip_length_bucket,
        subtitle_template_id=job.subtitle_template_id,
        aspect_ratio=job.aspect_ratio,
        source_range_start_seconds=float(range_start) if range_start is not None else None,
        source_range_end_seconds=float(range_end) if range_end is not None else None,
        source_trim_seconds=job.source_trim_seconds,
        status=job.status,
        created_clip_ids=list(job.created_clip_ids or []),
        error_message=job.error_message,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _source_duration_seconds(meta: dict | None) -> int | None:
    if not isinstance(meta, dict):
        return None
    raw = meta.get("duration_seconds")
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _source_thumbnail_url(meta: dict | None) -> str | None:
    if not isinstance(meta, dict):
        return None
    raw = meta.get("thumbnail_url") or meta.get("thumbnail")
    value = str(raw or "").strip()
    return value or None


def _youtube_thumbnail_url(url: str | None) -> str | None:
    video_id = _extract_youtube_video_id(url or "")
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else None


def _source_range_from_body(body: RepurposeJobCreate) -> tuple[float, float | None]:
    start = float(body.source_range_start_seconds or 0)
    end_raw = body.source_range_end_seconds
    if end_raw is None and body.source_trim_seconds is not None:
        end_raw = float(body.source_trim_seconds)
    end = float(end_raw) if end_raw is not None else None
    if end is not None and end <= start:
        raise HTTPException(status_code=400, detail="source range end must be after start")
    return start, end


def _source_meta_with_range(
    source_meta: dict | None,
    *,
    start: float,
    end: float | None,
) -> dict | None:
    meta = dict(source_meta or {})
    if start > 0:
        meta["source_range_start_seconds"] = start
    else:
        meta.pop("source_range_start_seconds", None)
    if end is not None:
        meta["source_range_end_seconds"] = end
    else:
        meta.pop("source_range_end_seconds", None)
    return meta or None


def _resolve_youtube_media_url(url: str) -> str:
    """
    Resolve a YouTube page URL to a direct media stream URL using yt-dlp.
    Mirrors reelcut's URL-import behavior where source ingestion starts from a URL.
    """
    try:
        return resolve_youtube_page_to_stream_url(url)
    except YoutubeStreamResolveError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _create_source_video(
    db: Session,
    *,
    current_user: User,
    project_id: int,
    source_url: str,
    name: str | None,
    ingest_page_url: str | None = None,
    thumbnail_url: str | None = None,
) -> Video:
    _project_with_write_access(project_id, db, current_user)

    latest_version = (
        db.query(Video)
        .filter(Video.project_id == project_id)
        .order_by(Video.version.desc())
        .first()
    )
    version = 1 if latest_version is None else (latest_version.version or 0) + 1

    page = (ingest_page_url or "").strip() or None
    video = Video(
        project_id=project_id,
        name=(name or "Repurpose source").strip()[:255],
        version=version,
        file_path=source_url,
        ingest_page_url=page,
        thumbnail_url=thumbnail_url,
        uploader_id=current_user.id,
        status="in_progress",
    )
    db.add(video)
    db.flush()

    vt = db.query(VideoTranscription).filter(VideoTranscription.video_id == video.id).first()
    if vt is None:
        db.add(VideoTranscription(video_id=video.id, status="pending"))
    db.commit()

    return video


# --- Suggestions ---------------------------------------------------------


@router.post(
    "/videos/{video_id}/suggest-clips",
    response_model=SuggestClipsResponse,
)
def suggest_clips_endpoint(
    video_id: int,
    body: SuggestClipsRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    video = _video_with_access(video_id, db, current_user)
    vt = (
        db.query(VideoTranscription)
        .filter(VideoTranscription.video_id == video_id)
        .first()
    )
    if vt is None or vt.status != "completed" or not vt.segments:
        return SuggestClipsResponse(
            suggestions=[],
            transcription_ready=False,
            video_duration=float(video.duration) if video.duration else None,
        )

    opts = body or SuggestClipsRequest()
    suggestions = run_suggest_clips(
        list(vt.segments),
        min_duration=opts.min_duration,
        max_duration=opts.max_duration,
        max_suggestions=opts.max_suggestions,
        video_duration=float(video.duration) if video.duration else None,
    )
    return SuggestClipsResponse(
        suggestions=[s.to_dict() for s in suggestions],
        transcription_ready=True,
        video_duration=float(video.duration) if video.duration else None,
    )


# --- Wizard metadata / defaults / jobs -----------------------------------


@router.post("/repurpose/youtube-metadata", response_model=YoutubeMetadataOut)
def fetch_youtube_metadata(
    body: YoutubeMetadataRequest,
    current_user: User = Depends(get_current_user),  # noqa: ARG001
):
    url = body.url.strip()
    video_id = _extract_youtube_video_id(url)
    if not video_id or not re.match(r"^[A-Za-z0-9_-]{6,}$", video_id):
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")

    embed_url = f"https://www.youtube.com/embed/{video_id}"
    oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"

    title = None
    thumbnail_url = None
    channel_title = None
    try:
        with urlopen(oembed_url, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            title = data.get("title")
            thumbnail_url = data.get("thumbnail_url")
            channel_title = data.get("author_name")
    except Exception:  # noqa: BLE001
        logger.info("youtube metadata fetch failed for %s", url)

    try:
        media = fetch_youtube_page_metadata(url)
        title = title or media.get("title")
        thumbnail_url = thumbnail_url or media.get("thumbnail")
        channel_title = channel_title or media.get("channel") or media.get("uploader")
        duration_seconds = media.get("duration")
    except Exception:  # noqa: BLE001
        duration_seconds = None

    return YoutubeMetadataOut(
        url=url,
        title=title,
        thumbnail_url=thumbnail_url,
        channel_title=channel_title,
        duration_seconds=int(duration_seconds) if duration_seconds else None,
        provider="youtube",
        embed_url=embed_url,
    )


@router.get("/repurpose/defaults", response_model=RepurposeUserDefaultsOut)
def get_repurpose_defaults(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = (
        db.query(RepurposeUserDefaults)
        .filter(RepurposeUserDefaults.user_id == current_user.id)
        .first()
    )
    if row is None:
        return RepurposeUserDefaultsOut(
            clip_mode="basic",
            default_prompt=None,
            genres=[],
            clip_length_bucket="lt_30",
            subtitle_template_id=None,
            aspect_ratio="9:16",
            source_trim_seconds=None,
        )
    return RepurposeUserDefaultsOut(
        clip_mode=row.clip_mode,
        default_prompt=row.default_prompt,
        genres=list(row.genres or []),
        clip_length_bucket=row.clip_length_bucket,
        subtitle_template_id=row.subtitle_template_id,
        aspect_ratio=row.aspect_ratio,
        source_trim_seconds=row.source_trim_seconds,
    )


@router.put("/repurpose/defaults", response_model=RepurposeUserDefaultsOut)
def update_repurpose_defaults(
    body: RepurposeUserDefaultsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = (
        db.query(RepurposeUserDefaults)
        .filter(RepurposeUserDefaults.user_id == current_user.id)
        .first()
    )
    if row is None:
        row = RepurposeUserDefaults(user_id=current_user.id)
        db.add(row)
    row.clip_mode = body.clip_mode
    row.default_prompt = body.default_prompt
    row.genres = body.genres
    row.clip_length_bucket = body.clip_length_bucket
    row.subtitle_template_id = body.subtitle_template_id
    row.aspect_ratio = body.aspect_ratio
    row.source_trim_seconds = body.source_trim_seconds
    db.commit()
    return RepurposeUserDefaultsOut(
        clip_mode=row.clip_mode,
        default_prompt=row.default_prompt,
        genres=list(row.genres or []),
        clip_length_bucket=row.clip_length_bucket,
        subtitle_template_id=row.subtitle_template_id,
        aspect_ratio=row.aspect_ratio,
        source_trim_seconds=row.source_trim_seconds,
    )


@router.post("/repurpose/jobs", response_model=RepurposeJobOut, status_code=201)
def create_repurpose_job(
    body: RepurposeJobCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project_id = body.project_id
    video_id = body.video_id
    resolved_source_file_url = body.source_file_url
    range_start, range_end = _source_range_from_body(body)
    source_meta = _source_meta_with_range(
        body.source_meta,
        start=range_start,
        end=range_end,
    )

    if body.source_mode == "project_video":
        if video_id is None:
            raise HTTPException(status_code=400, detail="video_id is required for project_video source")
        video = _video_with_access(video_id, db, current_user)
        _project_with_write_access(video.project_id, db, current_user)
        if project_id is None:
            project_id = video.project_id
    elif body.source_mode == "youtube_url":
        if not body.youtube_url:
            raise HTTPException(status_code=400, detail="youtube_url is required for youtube source")
        if project_id is None:
            raise HTTPException(status_code=400, detail="project_id is required for youtube source")
        _project_with_write_access(project_id, db, current_user)
        db.rollback()
        resolved_source_file_url = _resolve_youtube_media_url(body.youtube_url)
        src_video = _create_source_video(
            db,
            current_user=current_user,
            project_id=project_id,
            source_url=resolved_source_file_url,
            name=body.source_title or "YouTube source",
            ingest_page_url=body.youtube_url.strip(),
            thumbnail_url=_source_thumbnail_url(source_meta) or _youtube_thumbnail_url(body.youtube_url),
        )
        src_video.duration = _source_duration_seconds(source_meta)
        video_id = src_video.id
    elif body.source_mode == "upload" and not body.source_file_url:
        raise HTTPException(status_code=400, detail="source_file_url is required for upload source")
    elif body.source_mode == "upload":
        if project_id is None:
            raise HTTPException(status_code=400, detail="project_id is required for upload source")
        src_video = _create_source_video(
            db,
            current_user=current_user,
            project_id=project_id,
            source_url=body.source_file_url,
            name=body.source_title or "Uploaded source",
            thumbnail_url=_source_thumbnail_url(source_meta),
        )
        src_video.duration = _source_duration_seconds(source_meta)
        video_id = src_video.id

    if body.clip_mode == "clip_anything" and not body.clip_anything_prompt:
        raise HTTPException(status_code=400, detail="clip_anything_prompt is required for clip_anything mode")

    job = RepurposeJob(
        user_id=current_user.id,
        project_id=project_id,
        video_id=video_id,
        source_mode=body.source_mode,
        source_url=body.youtube_url if body.source_mode == "youtube_url" else None,
        source_file_url=resolved_source_file_url,
        source_title=body.source_title,
        source_meta=source_meta,
        clip_mode=body.clip_mode,
        clip_anything_prompt=body.clip_anything_prompt,
        genres=body.genres,
        clip_length_bucket=body.clip_length_bucket,
        subtitle_template_id=body.subtitle_template_id,
        aspect_ratio=body.aspect_ratio,
        source_trim_seconds=int(range_end) if range_end is not None else None,
        status="processing" if video_id is not None else "queued",
    )
    db.add(job)

    if body.save_as_default:
        defaults = (
            db.query(RepurposeUserDefaults)
            .filter(RepurposeUserDefaults.user_id == current_user.id)
            .first()
        )
        if defaults is None:
            defaults = RepurposeUserDefaults(user_id=current_user.id)
            db.add(defaults)
        defaults.clip_mode = body.clip_mode
        defaults.default_prompt = body.clip_anything_prompt
        defaults.genres = body.genres
        defaults.clip_length_bucket = body.clip_length_bucket
        defaults.subtitle_template_id = body.subtitle_template_id
        defaults.aspect_ratio = body.aspect_ratio
        defaults.source_trim_seconds = int(range_end) if range_end is not None else None

    db.commit()
    db.refresh(job)
    try:
        start_repurpose_processing(db, job.id)
        db.refresh(job)
    except Exception as exc:  # noqa: BLE001
        job_id = job.id
        logger.warning("Failed to start repurpose processing for job %s: %s", job_id, exc)
        db.rollback()
        fresh = db.query(RepurposeJob).filter(RepurposeJob.id == job_id).first()
        if fresh:
            job = fresh
    return _serialize_job(job)


@router.get("/repurpose/jobs", response_model=List[RepurposeJobOut])
def list_repurpose_jobs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (
        db.query(RepurposeJob)
        .filter(RepurposeJob.user_id == current_user.id)
        .order_by(RepurposeJob.created_at.desc())
        .all()
    )
    return [_serialize_job(r) for r in rows]


# --- Clip CRUD -----------------------------------------------------------


@router.post("/clips", response_model=ClipOut, status_code=201)
def create_clip(
    body: ClipCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    video = _video_with_access(body.video_id, db, current_user)
    if body.end_time <= body.start_time:
        raise HTTPException(status_code=400, detail="end_time must be greater than start_time")
    if video.duration and body.end_time > float(video.duration) + 2:
        raise HTTPException(status_code=400, detail="end_time exceeds video duration")

    name = body.name or (body.transcript_text[:60] if body.transcript_text else "Untitled clip")
    raw_cuts = [c.model_dump() for c in body.cuts] if body.cuts else None
    cuts = normalize_cuts(
        raw_cuts,
        fallback_start=float(body.start_time),
        fallback_end=float(body.end_time),
    )
    outer_start, outer_end = cuts_bounds(cuts)
    clip = Clip(
        video_id=body.video_id,
        user_id=current_user.id,
        name=name,
        start_time=outer_start,
        end_time=outer_end,
        cuts=cuts,
        duration_seconds=cuts_total_duration(cuts),
        aspect_ratio=body.aspect_ratio or "9:16",
        virality_score=body.virality_score,
        status="draft",
        is_ai_suggested=bool(body.is_ai_suggested),
        suggestion_reason=body.suggestion_reason,
        hooks_matched=body.hooks_matched,
        transcript_text=body.transcript_text,
        transcript_highlights=_normalize_clip_transcript_highlights(
            body.transcript_highlights, current_user
        ),
        transcript_comments=_normalize_clip_transcript_comments(
            body.transcript_comments, current_user
        ),
    )
    db.add(clip)
    db.commit()
    db.refresh(clip)
    _ensure_style(db, clip)
    db.refresh(clip)

    _auto_render_clip(db, clip)
    return _serialize_clip(clip)


@router.get("/clips", response_model=List[ClipOut])
def list_clips(
    video_id: Optional[int] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = (
        db.query(Clip)
        .options(joinedload(Clip.style))
        .join(Video, Video.id == Clip.video_id)
        .join(Project, Project.id == Video.project_id)
    )
    if video_id is not None:
        _video_with_access(video_id, db, current_user)
        q = q.filter(Clip.video_id == video_id)
    else:
        q = q.filter(Clip.user_id == current_user.id)
    if status_filter:
        q = q.filter(Clip.status == status_filter)
    rows = q.order_by(Clip.created_at.desc()).all()
    return [_serialize_clip(c) for c in rows]


@router.get("/clips/{clip_id}", response_model=ClipOut)
def get_clip(
    clip_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    clip = _clip_with_access(clip_id, db, current_user)
    return _serialize_clip(clip)


@router.put("/clips/{clip_id}", response_model=ClipOut)
def update_clip(
    clip_id: int,
    body: ClipUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    clip = _clip_with_access(clip_id, db, current_user)
    prior_cuts = list(clip.cuts or [])
    prior_start = float(clip.start_time)
    prior_end = float(clip.end_time)

    if body.name is not None:
        clip.name = body.name
    if body.aspect_ratio is not None:
        clip.aspect_ratio = body.aspect_ratio
    if body.transcript_text is not None:
        clip.transcript_text = body.transcript_text
    if body.transcript_highlights is not None:
        clip.transcript_highlights = _normalize_clip_transcript_highlights(
            body.transcript_highlights, current_user
        )
    if body.transcript_comments is not None:
        clip.transcript_comments = _normalize_clip_transcript_comments(
            body.transcript_comments, current_user
        )

    # Cuts is the source of truth when provided; otherwise fall back to
    # legacy single-range updates via start_time/end_time.
    if body.cuts is not None:
        raw = [c.model_dump() for c in body.cuts]
        cuts = normalize_cuts(raw, fallback_start=prior_start, fallback_end=prior_end)
    else:
        new_start = float(body.start_time) if body.start_time is not None else prior_start
        new_end = float(body.end_time) if body.end_time is not None else prior_end
        if new_end <= new_start:
            raise HTTPException(status_code=400, detail="end_time must be greater than start_time")
        cuts = normalize_cuts(
            [{"start": new_start, "end": new_end}],
            fallback_start=new_start,
            fallback_end=new_end,
        )

    outer_start, outer_end = cuts_bounds(cuts)
    clip.cuts = cuts
    clip.start_time = outer_start
    clip.end_time = outer_end
    clip.duration_seconds = cuts_total_duration(cuts)

    cuts_changed = _cuts_differ(prior_cuts, cuts)
    if cuts_changed and clip.status == "ready":
        clip.status = "draft"
        clip.storage_path = None

    db.commit()
    db.refresh(clip)
    return _serialize_clip(clip)


def _cuts_differ(a: list, b: list) -> bool:
    if len(a) != len(b):
        return True
    for x, y in zip(a, b):
        xs = float(x.get("start", 0))
        xe = float(x.get("end", 0))
        ys = float(y.get("start", 0))
        ye = float(y.get("end", 0))
        if abs(xs - ys) > 1e-3 or abs(xe - ye) > 1e-3:
            return True
    return False


def _auto_render_clip(db: Session, clip: Clip) -> None:
    """Best-effort: kick off a render so each clip has its own MP4 + thumbnail.

    Uses the background queue when Redis is available. Swallows failures so
    the CRUD response stays fast; the user can still click Render manually.

    Also extracts a fast single-frame thumbnail from the source synchronously
    so the gallery has a real image before the render finishes.
    """
    try:
        from app.jobs.queue import enqueue_clip_render_job
        from app.services.clip_renderer import fast_thumbnail_for_clip

        fast_thumbnail_for_clip(db, clip.id)

        clip.status = "queued"
        clip.render_progress = 0
        clip.render_error = None
        db.commit()
        job_id = enqueue_clip_render_job(clip.id)
        if job_id is None:
            clip.status = "draft"
            db.commit()
            return
        clip.rq_job_id = job_id
        db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("auto-render enqueue failed for clip %s", clip.id)
        try:
            clip.status = "draft"
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()


@router.delete("/clips/{clip_id}", status_code=204)
def delete_clip(
    clip_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    clip = _clip_with_access(clip_id, db, current_user)
    db.delete(clip)
    db.commit()


# --- Clip style ----------------------------------------------------------


@router.get("/clips/{clip_id}/style", response_model=ClipStyleOut)
def get_clip_style(
    clip_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    clip = _clip_with_access(clip_id, db, current_user)
    style = _ensure_style(db, clip)
    return ClipStyleOut.model_validate(style)


@router.put("/clips/{clip_id}/style", response_model=ClipStyleOut)
def update_clip_style(
    clip_id: int,
    body: ClipStyleUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    clip = _clip_with_access(clip_id, db, current_user)
    style = _ensure_style(db, clip)
    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(style, k, v)
    # Re-rendering required if style changes.
    if clip.status == "ready":
        clip.status = "draft"
        clip.storage_path = None
    db.commit()
    db.refresh(style)
    return ClipStyleOut.model_validate(style)


@router.post("/clips/{clip_id}/style/apply-template/{template_id}", response_model=ClipStyleOut)
def apply_template(
    clip_id: int,
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    clip = _clip_with_access(clip_id, db, current_user)
    tpl = db.query(ClipTemplate).filter(ClipTemplate.id == template_id).first()
    if tpl is None:
        raise HTTPException(status_code=404, detail="Template not found")
    if tpl.user_id is not None and tpl.user_id != current_user.id and not tpl.is_public:
        raise HTTPException(status_code=403, detail="Template not accessible")
    style = _ensure_style(db, clip)
    for k, v in (tpl.style_config or {}).items():
        if hasattr(style, k):
            setattr(style, k, v)
    tpl.usage_count = (tpl.usage_count or 0) + 1
    if clip.status == "ready":
        clip.status = "draft"
        clip.storage_path = None
    db.commit()
    db.refresh(style)
    return ClipStyleOut.model_validate(style)


# --- Captions ------------------------------------------------------------


def _blocks_for_clip(clip: Clip, db: Session):
    vt = (
        db.query(VideoTranscription)
        .filter(VideoTranscription.video_id == clip.video_id)
        .first()
    )
    if not vt or not vt.segments:
        return []
    style = clip.style
    wpl = style.caption_words_per_line if style else 3
    ml = style.caption_max_lines if style else 2
    uc = bool(style.caption_uppercase) if style else False
    cuts = normalize_cuts(
        list(clip.cuts or []),
        fallback_start=float(clip.start_time),
        fallback_end=float(clip.end_time),
    )
    return blocks_from_cuts(
        list(vt.segments),
        cuts=cuts,
        words_per_line=wpl,
        max_lines=ml,
        uppercase=uc,
    )


@router.get("/clips/{clip_id}/captions.srt")
def get_clip_captions_srt(
    clip_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from fastapi.responses import PlainTextResponse

    clip = _clip_with_access(clip_id, db, current_user)
    blocks = _blocks_for_clip(clip, db)
    return PlainTextResponse(to_srt(blocks), media_type="application/x-subrip")


@router.get("/clips/{clip_id}/captions.vtt")
def get_clip_captions_vtt(
    clip_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from fastapi.responses import PlainTextResponse

    clip = _clip_with_access(clip_id, db, current_user)
    blocks = _blocks_for_clip(clip, db)
    return PlainTextResponse(to_vtt(blocks), media_type="text/vtt")


# --- Render / status / download -----------------------------------------


@router.post("/clips/{clip_id}/render", response_model=ClipRenderResponse)
def render_clip_endpoint(
    clip_id: int,
    body: ClipRenderRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    clip = _clip_with_access(clip_id, db, current_user)
    if clip.status in ("queued", "rendering"):
        raise HTTPException(status_code=409, detail="Clip is already rendering")

    from app.jobs.queue import enqueue_clip_render_job

    clip.status = "queued"
    clip.render_progress = 0
    clip.render_error = None
    if body and body.preset:
        clip.preset = body.preset
    db.commit()

    job_id = enqueue_clip_render_job(clip.id)
    if job_id is None:
        # No Redis configured — render synchronously (dev convenience).
        from app.services.clip_renderer import render_clip as _render

        try:
            clip.status = "rendering"
            db.commit()
            _render(db, clip.id)
            clip.status = "ready"
            clip.render_progress = 100
            db.commit()
        except Exception as e:  # noqa: BLE001
            logger.exception("inline clip render failed")
            clip.status = "failed"
            clip.render_error = str(e)[:4000]
            db.commit()
            raise HTTPException(status_code=500, detail=f"Render failed: {e}")
    else:
        clip.rq_job_id = job_id
        db.commit()

    return ClipRenderResponse(clip_id=clip.id, status=clip.status, rq_job_id=clip.rq_job_id)


@router.get("/clips/{clip_id}/status")
def get_clip_status(
    clip_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    clip = _clip_with_access(clip_id, db, current_user)
    return {
        "clip_id": clip.id,
        "status": clip.status,
        "progress": clip.render_progress,
        "error": clip.render_error,
        "storage_path": clip.storage_path,
        "thumbnail_url": clip.thumbnail_url,
    }


@router.get("/clips/{clip_id}/download")
def get_clip_download(
    clip_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    clip = _clip_with_access(clip_id, db, current_user)
    if clip.status != "ready" or not clip.storage_path:
        raise HTTPException(status_code=409, detail="Clip is not ready for download")
    return {"url": clip.storage_path}


# --- Templates -----------------------------------------------------------


@router.get("/clip-templates", response_model=List[TemplateOut])
def list_templates(
    scope: str = Query("all", pattern="^(all|mine|public)$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = db.query(ClipTemplate)
    if scope == "mine":
        q = q.filter(ClipTemplate.user_id == current_user.id)
    elif scope == "public":
        q = q.filter(ClipTemplate.user_id.is_(None) | (ClipTemplate.is_public == True))  # noqa: E712
    else:
        q = q.filter(
            (ClipTemplate.user_id == current_user.id)
            | (ClipTemplate.user_id.is_(None))
            | (ClipTemplate.is_public == True)  # noqa: E712
        )
    rows = q.order_by(ClipTemplate.usage_count.desc(), ClipTemplate.id.asc()).all()
    return [TemplateOut.model_validate(t) for t in rows]


@router.post("/clip-templates", response_model=TemplateOut, status_code=201)
def create_template(
    body: TemplateCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tpl = ClipTemplate(
        user_id=current_user.id,
        name=body.name,
        category=body.category,
        is_public=body.is_public,
        preview_url=body.preview_url,
        style_config=body.style_config,
    )
    db.add(tpl)
    db.commit()
    db.refresh(tpl)
    return TemplateOut.model_validate(tpl)


@router.put("/clip-templates/{template_id}", response_model=TemplateOut)
def update_template(
    template_id: int,
    body: TemplateUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tpl = db.query(ClipTemplate).filter(ClipTemplate.id == template_id).first()
    if tpl is None:
        raise HTTPException(status_code=404, detail="Template not found")
    if tpl.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Cannot edit this template")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(tpl, k, v)
    db.commit()
    db.refresh(tpl)
    return TemplateOut.model_validate(tpl)


@router.delete("/clip-templates/{template_id}", status_code=204)
def delete_template(
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tpl = db.query(ClipTemplate).filter(ClipTemplate.id == template_id).first()
    if tpl is None:
        raise HTTPException(status_code=404, detail="Template not found")
    if tpl.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Cannot delete this template")
    db.delete(tpl)
    db.commit()
