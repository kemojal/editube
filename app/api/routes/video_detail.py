import time

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload, selectinload

from app.db.database import get_db
from app.db.models import Project, Video, User
from app.api.models.videos import VideoWithProjectResponse, VideoDetailResponse
from app.api.video_payload import video_detail_dict, video_versions_payload
from app.utils.security import get_current_user
from app.services.transcription_enqueue import prepare_and_enqueue_transcription
from app.services.project_access import assert_write_project_content, can_access_project
from app.services.youtube_stream_resolve import (
    YoutubeStreamResolveError,
    resolve_youtube_page_to_stream_url,
    should_refresh_stream_url,
    STREAM_REFRESH_MIN_REMAINING_SEC,
    stream_url_expire_at as _stream_url_expire_at,
)

router = APIRouter(
    prefix="/videos",
    tags=["Video Detail"],
)


def _video_detail(
    video: Video,
    viewer_user_id: int | None = None,
    *,
    db: Session | None = None,
    db_project: Project | None = None,
) -> dict:
    return video_detail_dict(
        video, viewer_user_id, db=db, db_project=db_project
    )


def _video_with_project_payload(
    db: Session, db_video: Video, viewer_user_id: int | None = None
) -> dict:
    db_project = db.query(Project).filter(Project.id == db_video.project_id).first()
    detail = _video_detail(
        db_video, viewer_user_id, db=db, db_project=db_project
    )
    detail["project"] = db_project
    detail["versions"] = video_versions_payload(db, db_video)
    return detail


@router.post("/{video_id}/transcription", response_model=VideoWithProjectResponse)
def start_video_transcription(
    video_id: int,
    force: bool = Query(
        False,
        description="If true, reset stuck queued/processing and enqueue again (worker crash, RQ timeout, expired URL).",
    ),
    language: str | None = Query(
        None,
        description="ISO 639-1 spoken language for this run (e.g. 'en'). Omit to keep the "
        "video's existing language selection; 'auto'/'' resets to auto-detect.",
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Enqueue transcription for this video (legacy videos, retries, or first run)."""
    db_video = (
        db.query(Video)
        .options(
            joinedload(Video.uploader),
            joinedload(Video.transcription),
            selectinload(Video.comments),
            selectinload(Video.annotations),
        )
        .filter(Video.id == video_id)
        .first()
    )
    if not db_video:
        raise HTTPException(status_code=404, detail="Video not found")

    db_project = db.query(Project).filter(Project.id == db_video.project_id).first()
    if not can_access_project(db, current_user.id, db_project):
        raise HTTPException(status_code=403, detail="Not authorized to access this video")
    assert_write_project_content(db, current_user, db_project)

    prepare_and_enqueue_transcription(db, video_id, force=force, language=language)

    db_video = (
        db.query(Video)
        .options(
            joinedload(Video.uploader),
            joinedload(Video.transcription),
            selectinload(Video.comments),
            selectinload(Video.annotations),
        )
        .filter(Video.id == video_id)
        .first()
    )
    return _video_with_project_payload(db, db_video, current_user.id)


@router.get("/{video_id}", response_model=VideoWithProjectResponse)
def get_video_by_id(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db_video = (
        db.query(Video)
        .options(
            joinedload(Video.uploader),
            joinedload(Video.transcription),
            selectinload(Video.comments),
            selectinload(Video.annotations),
        )
        .filter(Video.id == video_id)
        .first()
    )
    if not db_video:
        raise HTTPException(status_code=404, detail="Video not found")

    db_project = db.query(Project).filter(Project.id == db_video.project_id).first()
    if not can_access_project(db, current_user.id, db_project):
        raise HTTPException(status_code=403, detail="Not authorized to access this video")

    return _video_with_project_payload(db, db_video, current_user.id)


@router.post("/{video_id}/stream/refresh")
def refresh_video_stream(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Re-resolve a YouTube-sourced video's direct stream URL (`file_path`) via
    yt-dlp, using the canonical `ingest_page_url`. googlevideo stream URLs
    carry an `expire=<unix>` query param and go dead ~6h after issuance,
    causing players to render black.

    Only valid for videos that were ingested from YouTube (`ingest_page_url`
    set) — everything else 409s. Rate-guarded: if the current `file_path`'s
    `expire` param is still comfortably in the future, returns the existing
    URL without spawning yt-dlp.
    """
    db_video = db.query(Video).filter(Video.id == video_id).first()
    if not db_video:
        raise HTTPException(status_code=404, detail="Video not found")

    db_project = db.query(Project).filter(Project.id == db_video.project_id).first()
    if not can_access_project(db, current_user.id, db_project):
        raise HTTPException(status_code=403, detail="Not authorized to access this video")

    if not (db_video.ingest_page_url or "").strip():
        raise HTTPException(
            status_code=409,
            detail="This video has no source YouTube URL to re-resolve a stream from.",
        )

    # Re-resolve when the URL is expiring OR when it is a DASH stream that
    # cannot play on its own — a video-only stream is just as unplayable as an
    # expired one, and stays that way for hours if only expiry is checked.
    if not should_refresh_stream_url(db_video.file_path):
        return {"video_id": db_video.id, "file_path": db_video.file_path}

    try:
        new_stream_url = resolve_youtube_page_to_stream_url(db_video.ingest_page_url)
    except YoutubeStreamResolveError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    db_video.file_path = new_stream_url
    db.commit()
    db.refresh(db_video)

    return {"video_id": db_video.id, "file_path": db_video.file_path}


@router.put("/{video_id}/status", response_model=VideoDetailResponse)
def update_video_status(
    video_id: int,
    data: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    valid_statuses = ("in_progress", "in_review", "approved", "needs_changes")
    status = data.get("status")
    if status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {', '.join(valid_statuses)}")

    db_video = (
        db.query(Video)
        .options(
            joinedload(Video.uploader),
            joinedload(Video.transcription),
            selectinload(Video.comments),
            selectinload(Video.annotations),
        )
        .filter(Video.id == video_id)
        .first()
    )
    if not db_video:
        raise HTTPException(status_code=404, detail="Video not found")

    db_project = db.query(Project).filter(Project.id == db_video.project_id).first()
    if not can_access_project(db, current_user.id, db_project):
        raise HTTPException(status_code=403, detail="Not authorized")
    assert_write_project_content(db, current_user, db_project)

    db_video.status = status
    db.commit()
    db.refresh(db_video)
    db_video = (
        db.query(Video)
        .options(
            joinedload(Video.uploader),
            joinedload(Video.transcription),
            selectinload(Video.comments),
            selectinload(Video.annotations),
        )
        .filter(Video.id == video_id)
        .first()
    )
    return _video_detail(
        db_video, current_user.id, db=db, db_project=db_project
    )
