from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form, Query
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload, selectinload
from typing import List, Optional
import logging

from app.db.database import get_db
from app.db.models import Project, Video, VideoTranscription, User, Folder
from app.services.project_access import assert_write_project_content, can_access_project
from app.services.storage_policy import assert_storage_upload_allowed
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
from app.utils.cloudinary import upload_file_to_cloudinary_with_meta
from app.services.transcription_enqueue import prepare_and_enqueue_transcription
from app.services.activity import log_activity

router = APIRouter(
    prefix="/projects/{project_id}/videos",
    tags=["Videos"],
)

logger = logging.getLogger(__name__)


def _upload_file_size_bytes(video_file: UploadFile) -> int:
    stream = video_file.file
    if not hasattr(stream, "seek") or not hasattr(stream, "tell"):
        return 0
    current = stream.tell()
    stream.seek(0, 2)
    size = int(stream.tell() or 0)
    stream.seek(current)
    return size


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


@router.post("/", response_model=VideoDetailResponse)
def upload_video(
    project_id: int,
    video_file: UploadFile = File(...),
    name: str = Form(...),
    description: Optional[str] = Form(None),
    folder_id: Optional[int] = Form(None),
    # When set, this upload becomes the next version in that video's chain.
    version_of: Optional[int] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_access_project(db, current_user.id, db_project):
        raise HTTPException(status_code=403, detail="Not authorized to upload videos to this project")
    assert_write_project_content(db, current_user, db_project)

    # Resolve the version chain. A new version inherits its predecessor's group
    # and folder; a fresh upload starts its own chain.
    base_video: Optional[Video] = None
    if version_of is not None:
        base_video = (
            db.query(Video)
            .filter(Video.id == version_of, Video.project_id == project_id)
            .first()
        )
        if not base_video:
            raise HTTPException(status_code=404, detail="Version target video not found in this project")
        folder_id = base_video.folder_id

    if folder_id is not None:
        folder = db.query(Folder).filter(Folder.id == folder_id, Folder.project_id == project_id).first()
        if not folder:
            raise HTTPException(status_code=404, detail="Folder not found in this project")

    incoming_size = _upload_file_size_bytes(video_file)
    try:
        assert_storage_upload_allowed(
            db,
            user=current_user,
            workspace_id=db_project.workspace_id,
            incoming_bytes=incoming_size,
        )
    except ValueError:
        raise HTTPException(
            status_code=402,
            detail=(
                "Storage cap reached and grace period ended. "
                "Upgrade plan or add storage to continue uploads."
            ),
        )

    upload = upload_file_to_cloudinary_with_meta(video_file)
    file_url = str(upload["url"])
    uploaded_size = int(upload.get("bytes") or incoming_size or 0)

    import uuid as _uuid

    if base_video is not None:
        version_group_id = base_video.version_group_id or _uuid.uuid4().hex
        # Backfill the base's group if it predates the version_group_id column.
        if not base_video.version_group_id:
            base_video.version_group_id = version_group_id
        latest_in_group = (
            db.query(Video)
            .filter(
                Video.project_id == project_id,
                Video.version_group_id == version_group_id,
            )
            .order_by(Video.version.desc())
            .first()
        )
        version = 1 if not latest_in_group else (latest_in_group.version or 0) + 1
    else:
        version_group_id = _uuid.uuid4().hex
        version = 1

    db_video = Video(
        project_id=project_id,
        folder_id=folder_id,
        name=name,
        description=description,
        version=version,
        version_group_id=version_group_id,
        file_path=file_url,
        size_bytes=uploaded_size,
        uploader_id=current_user.id,
    )
    db.add(db_video)
    db.flush()

    db_tr = VideoTranscription(video_id=db_video.id, status="pending")
    db.add(db_tr)
    log_activity(
        db,
        user_id=current_user.id,
        project_id=project_id,
        action="video_uploaded",
        meta={"video_name": name, "video_id": db_video.id},
    )
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

    # Auto-generate review proxy if enabled
    try:
        from app.services.proxy_service import auto_proxy_on_upload
        auto_proxy_on_upload(db, db_video.id)
    except Exception as e:
        logger.warning("Auto-proxy not triggered for video %s: %s", db_video.id, e)

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
    return _video_detail(
        db_video, current_user.id, db=db, db_project=db_project
    )


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
    if not can_access_project(db, current_user.id, db_project):
        raise HTTPException(status_code=403, detail="Not authorized to access this video")

    # All versions in this video's chain (same version_group_id), newest first.
    # Fall back to just this video when the group is somehow unset.
    if db_video.version_group_id:
        all_versions = (
            db.query(Video)
            .options(joinedload(Video.uploader))
            .filter(
                Video.project_id == project_id,
                Video.version_group_id == db_video.version_group_id,
            )
            .order_by(Video.version.desc())
            .all()
        )
    else:
        all_versions = [db_video]

    from app.db.models import Comment as _Comment

    version_ids = [v.id for v in all_versions]
    comment_counts: dict[int, int] = {}
    if version_ids:
        rows = (
            db.query(_Comment.video_id, func.count(_Comment.id))
            .filter(_Comment.video_id.in_(version_ids))
            .group_by(_Comment.video_id)
            .all()
        )
        comment_counts = {vid: int(cnt) for vid, cnt in rows}

    detail = _video_detail(
        db_video, current_user.id, db=db, db_project=db_project
    )
    detail["project"] = db_project
    detail["versions"] = [
        {
            "id": v.id,
            "version": v.version,
            "name": v.name,
            "created_at": v.created_at,
            "thumbnail_url": v.thumbnail_url,
            "duration": v.duration,
            "comment_count": comment_counts.get(v.id, 0),
            "uploader_name": (v.uploader.name if v.uploader else None),
        }
        for v in all_versions
    ]
    return detail


@router.post("/{video_id}/transcription", response_model=VideoWithProjectResponse)
def start_project_video_transcription(
    project_id: int,
    video_id: int,
    force: bool = Query(
        False,
        description="If true, reset stuck queued/processing and enqueue again.",
    ),
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
        .filter(Video.id == video_id, Video.project_id == project_id)
        .first()
    )
    all_versions = (
        db.query(Video)
        .filter(Video.project_id == project_id)
        .order_by(Video.version.desc())
        .all()
    )
    detail = _video_detail(
        db_video, current_user.id, db=db, db_project=db_project
    )
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
    if not can_access_project(db, current_user.id, db_project):
        raise HTTPException(status_code=403, detail="Not authorized")
    assert_write_project_content(db, current_user, db_project)

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
    return _video_detail(
        db_video, current_user.id, db=db, db_project=db_project
    )
