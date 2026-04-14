from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form
from sqlalchemy.orm import Session, joinedload, selectinload
from typing import List, Optional
import logging

from app.db.database import get_db
from app.db.models import Project, Video, VideoTranscription, User, Folder
from app.api.video_payload import video_detail_dict
from app.api.models.videos import (
    VideoCreate,
    VideoUpdate,
    VideoStatusUpdate,
    VideoDetailResponse,
    VideoWithProjectResponse,
    VideoVersionSummary,
    UploaderResponse,
    ProjectSummary,
)
from app.utils.security import get_current_user
from app.utils.storage import upload_file, delete_file
from app.utils.cloudinary import upload_file_to_cloudinary
from app.services.transcription_enqueue import prepare_and_enqueue_transcription

router = APIRouter(
    prefix="/projects/{project_id}/videos",
    tags=["Videos"],
)

logger = logging.getLogger(__name__)


def _video_detail(video: Video) -> dict:
    return video_detail_dict(video)


@router.post("/", response_model=VideoDetailResponse)
def upload_video(
    project_id: int,
    video_file: UploadFile = File(...),
    name: str = Form(...),
    description: Optional[str] = Form(None),
    folder_id: Optional[int] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")
    if current_user not in [db_project.creator] + [c.user for c in db_project.collaborators]:
        raise HTTPException(status_code=403, detail="Not authorized to upload videos to this project")

    if folder_id is not None:
        folder = db.query(Folder).filter(Folder.id == folder_id, Folder.project_id == project_id).first()
        if not folder:
            raise HTTPException(status_code=404, detail="Folder not found in this project")

    file_url = upload_file_to_cloudinary(video_file)

    latest_version = db.query(Video).filter(Video.project_id == project_id).order_by(Video.version.desc()).first()
    version = 1 if not latest_version else latest_version.version + 1

    db_video = Video(
        project_id=project_id,
        folder_id=folder_id,
        name=name,
        description=description,
        version=version,
        file_path=file_url,
        uploader_id=current_user.id,
    )
    db.add(db_video)
    db.flush()

    db_tr = VideoTranscription(video_id=db_video.id, status="pending")
    db.add(db_tr)
    db.commit()

    try:
        from app.jobs.queue import enqueue_transcription_job

        if enqueue_transcription_job(db_video.id):
            row = (
                db.query(VideoTranscription)
                .filter(VideoTranscription.video_id == db_video.id)
                .first()
            )
            if row:
                row.status = "queued"
                db.commit()
    except Exception as e:
        logger.warning("Transcription job not enqueued for video %s: %s", db_video.id, e)

    db_video = (
        db.query(Video)
        .options(
            joinedload(Video.uploader),
            joinedload(Video.transcription),
            selectinload(Video.comments),
            selectinload(Video.annotations),
        )
        .filter(Video.id == db_video.id)
        .first()
    )
    return _video_detail(db_video)


@router.get("/{video_id}", response_model=VideoWithProjectResponse)
def get_video(
    project_id: int,
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
        .filter(Video.id == video_id, Video.project_id == project_id)
        .first()
    )
    if not db_video:
        raise HTTPException(status_code=404, detail="Video not found")
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if current_user not in [db_project.creator] + [c.user for c in db_project.collaborators]:
        raise HTTPException(status_code=403, detail="Not authorized to access this video")

    # Get all versions of this video in the project
    all_versions = (
        db.query(Video)
        .filter(Video.project_id == project_id)
        .order_by(Video.version.desc())
        .all()
    )

    detail = _video_detail(db_video)
    detail["project"] = db_project
    detail["versions"] = [
        {"id": v.id, "version": v.version, "name": v.name, "created_at": v.created_at}
        for v in all_versions
    ]
    return detail


@router.post("/{video_id}/transcription", response_model=VideoWithProjectResponse)
def start_project_video_transcription(
    project_id: int,
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Enqueue transcription for this video (same as POST /videos/{id}/transcription)."""
    db_video = (
        db.query(Video)
        .options(
            joinedload(Video.uploader),
            joinedload(Video.transcription),
            selectinload(Video.comments),
            selectinload(Video.annotations),
        )
        .filter(Video.id == video_id, Video.project_id == project_id)
        .first()
    )
    if not db_video:
        raise HTTPException(status_code=404, detail="Video not found")
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if current_user not in [db_project.creator] + [c.user for c in db_project.collaborators]:
        raise HTTPException(status_code=403, detail="Not authorized to access this video")

    prepare_and_enqueue_transcription(db, video_id)

    db_video = (
        db.query(Video)
        .options(
            joinedload(Video.uploader),
            joinedload(Video.transcription),
            selectinload(Video.comments),
            selectinload(Video.annotations),
        )
        .filter(Video.id == video_id, Video.project_id == project_id)
        .first()
    )
    all_versions = (
        db.query(Video)
        .filter(Video.project_id == project_id)
        .order_by(Video.version.desc())
        .all()
    )
    detail = _video_detail(db_video)
    detail["project"] = db_project
    detail["versions"] = [
        {"id": v.id, "version": v.version, "name": v.name, "created_at": v.created_at}
        for v in all_versions
    ]
    return detail


@router.put("/{video_id}/status", response_model=VideoDetailResponse)
def update_video_status(
    project_id: int,
    video_id: int,
    data: VideoStatusUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    valid_statuses = ("in_progress", "in_review", "approved", "needs_changes")
    if data.status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {', '.join(valid_statuses)}")

    db_video = (
        db.query(Video)
        .options(
            joinedload(Video.uploader),
            joinedload(Video.transcription),
            selectinload(Video.comments),
            selectinload(Video.annotations),
        )
        .filter(Video.id == video_id, Video.project_id == project_id)
        .first()
    )
    if not db_video:
        raise HTTPException(status_code=404, detail="Video not found")
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if current_user not in [db_project.creator] + [c.user for c in db_project.collaborators]:
        raise HTTPException(status_code=403, detail="Not authorized")

    db_video.status = data.status
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
        .filter(Video.id == video_id, Video.project_id == project_id)
        .first()
    )
    return _video_detail(db_video)
