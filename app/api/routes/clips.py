"""
Repurpose / short-clip API.

Shape mirrors reelcut's /clips domain but adapted to editube conventions:
- Integer IDs, Bearer-JWT auth, snake_case JSON.
- Source videos live in the existing videos table.
- Clips inherit access control from the source video's project.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional
import json
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

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
    blocks_from_segments,
    to_srt,
    to_vtt,
)
from app.services.project_access import can_access_project
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


def _extract_youtube_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if "youtu.be" in host:
        return parsed.path.strip("/") or None
    if "youtube.com" in host:
        if parsed.path == "/watch":
            return parse_qs(parsed.query).get("v", [None])[0]
        if parsed.path.startswith("/shorts/") or parsed.path.startswith("/embed/"):
            parts = [p for p in parsed.path.split("/") if p]
            return parts[1] if len(parts) > 1 else None
    return None


def _serialize_job(job: RepurposeJob) -> RepurposeJobOut:
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
        source_trim_seconds=job.source_trim_seconds,
        status=job.status,
        created_clip_ids=list(job.created_clip_ids or []),
        error_message=job.error_message,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


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

    return YoutubeMetadataOut(
        url=url,
        title=title,
        thumbnail_url=thumbnail_url,
        channel_title=channel_title,
        duration_seconds=None,
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

    if body.source_mode == "project_video":
        if video_id is None:
            raise HTTPException(status_code=400, detail="video_id is required for project_video source")
        video = _video_with_access(video_id, db, current_user)
        if project_id is None:
            project_id = video.project_id
    elif body.source_mode == "youtube_url":
        if not body.youtube_url:
            raise HTTPException(status_code=400, detail="youtube_url is required for youtube source")
    elif body.source_mode == "upload" and not body.source_file_url:
        raise HTTPException(status_code=400, detail="source_file_url is required for upload source")

    if body.clip_mode == "clip_anything" and not body.clip_anything_prompt:
        raise HTTPException(status_code=400, detail="clip_anything_prompt is required for clip_anything mode")

    job = RepurposeJob(
        user_id=current_user.id,
        project_id=project_id,
        video_id=video_id,
        source_mode=body.source_mode,
        source_url=body.youtube_url if body.source_mode == "youtube_url" else None,
        source_file_url=body.source_file_url,
        source_title=body.source_title,
        source_meta=body.source_meta,
        clip_mode=body.clip_mode,
        clip_anything_prompt=body.clip_anything_prompt,
        genres=body.genres,
        clip_length_bucket=body.clip_length_bucket,
        subtitle_template_id=body.subtitle_template_id,
        aspect_ratio=body.aspect_ratio,
        source_trim_seconds=body.source_trim_seconds,
        status="queued",
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
        defaults.source_trim_seconds = body.source_trim_seconds

    db.commit()
    db.refresh(job)
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
    clip = Clip(
        video_id=body.video_id,
        user_id=current_user.id,
        name=name,
        start_time=float(body.start_time),
        end_time=float(body.end_time),
        duration_seconds=float(body.end_time) - float(body.start_time),
        aspect_ratio=body.aspect_ratio or "9:16",
        virality_score=body.virality_score,
        status="draft",
        is_ai_suggested=bool(body.is_ai_suggested),
        suggestion_reason=body.suggestion_reason,
        hooks_matched=body.hooks_matched,
        transcript_text=body.transcript_text,
    )
    db.add(clip)
    db.commit()
    db.refresh(clip)
    _ensure_style(db, clip)
    db.refresh(clip)
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
    if body.name is not None:
        clip.name = body.name
    if body.aspect_ratio is not None:
        clip.aspect_ratio = body.aspect_ratio
    if body.start_time is not None:
        clip.start_time = float(body.start_time)
    if body.end_time is not None:
        clip.end_time = float(body.end_time)
    if clip.end_time <= clip.start_time:
        raise HTTPException(status_code=400, detail="end_time must be greater than start_time")
    clip.duration_seconds = float(clip.end_time) - float(clip.start_time)
    # Edit invalidates the render.
    if clip.status == "ready":
        clip.status = "draft"
        clip.storage_path = None
    db.commit()
    db.refresh(clip)
    return _serialize_clip(clip)


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
    return blocks_from_segments(
        list(vt.segments),
        clip_start=float(clip.start_time),
        clip_end=float(clip.end_time),
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
