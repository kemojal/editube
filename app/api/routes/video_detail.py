from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload, selectinload

from app.db.database import get_db
from app.db.models import Project, Video, User
from app.api.models.videos import VideoWithProjectResponse, VideoDetailResponse
from app.api.video_payload import video_detail_dict
from app.utils.security import get_current_user
from app.services.transcription_enqueue import prepare_and_enqueue_transcription
from app.services.project_access import assert_write_project_content, can_access_project

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
    all_versions = (
        db.query(Video)
        .filter(Video.project_id == db_video.project_id)
        .order_by(Video.version.desc())
        .all()
    )
    detail = _video_detail(
        db_video, viewer_user_id, db=db, db_project=db_project
    )
    detail["project"] = db_project
    detail["versions"] = [
        {"id": v.id, "version": v.version, "name": v.name, "created_at": v.created_at}
        for v in all_versions
    ]
    return detail


@router.post("/{video_id}/transcription", response_model=VideoWithProjectResponse)
def start_video_transcription(
    video_id: int,
    force: bool = Query(
        False,
        description="If true, reset stuck queued/processing and enqueue again (worker crash, RQ timeout, expired URL).",
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

    prepare_and_enqueue_transcription(db, video_id, force=force)

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
