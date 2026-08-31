from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form, Query
from sqlalchemy.orm import Session, joinedload
from typing import List, Optional
import logging

from app.db.database import get_db
from app.db.models import Comment, DriveImport, Project, Video, VideoTranscription, User, Folder
from app.services.project_access import assert_write_project_content, can_access_project
from app.services.storage_policy import assert_storage_upload_allowed
from app.api.video_payload import video_detail_dict, video_versions_payload
from app.services.video_versions import resolve_version_chain
from app.api.models.videos import (
    VideoCreate,
    VideoUpdate,
    VideoStatusUpdate,
    SendForReviewRequest,
    VideoDetailResponse,
    VideoWithProjectResponse,
    VideoVersionSummary,
    UploaderResponse,
    ProjectSummary,
    YoutubeVideoCreate,
    VideoFromUploadCreate,
)
from app.services.notifications import (
    TYPE_NEW_VERSION,
    TYPE_REVIEW_REQUESTED,
    NotificationSpec,
    emit_notifications,
    emit_notifications_sync,
)
from app.services.video_status import (
    DECISION_APPROVED,
    STATUS_APPROVED,
    STATUS_IN_REVIEW,
    IllegalStatusTransition,
    InvalidVideoStatus,
    apply_video_status,
    record_decision,
    supersede_open_decisions,
)
from app.services.comment_carry_forward import (
    carry_forward_open_change_requests,
    count_open_change_requests,
)
from app.utils.security import get_current_user
from app.utils.storage import upload_file, delete_file
from app.utils.cloudinary import upload_file_to_cloudinary_with_meta
from app.services.transcription_enqueue import prepare_and_enqueue_transcription
from app.services.activity import log_activity
from app.services.youtube_source_video import create_youtube_source_video
from app.services.youtube_stream_resolve import YoutubeStreamResolveError
from app.utils.language import normalize_language
from app.services.product_analytics import emit, emit_once
from app.services.ingest_service import record_ingested_video_result_use

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


def _finalize_project_video(
    db: Session,
    *,
    project_id: int,
    current_user: User,
    file_path: str,
    size_bytes: int,
    name: str,
    description: Optional[str],
    folder_id: Optional[int],
    version: int,
    version_group_id: str,
    language: Optional[str],
    activity_action: str,
    source_type: str = "upload",
    integration_import_id: int | None = None,
    version_notes: Optional[str] = None,
    base_video: Optional[Video] = None,
) -> Video:
    """
    Shared tail of "a file is now stored, register it as a project Video":
    creates the Video row (caller has already resolved folder_id/version/
    version_group_id and validated project access + storage quota), seeds +
    enqueues a pending transcription with `language`, logs activity, and
    fires the best-effort thumbnail + auto-proxy jobs.

    Used by both the multipart upload route (after the Cloudinary upload) and
    POST /projects/{project_id}/videos/from-upload (file already stored).
    """
    db_video = Video(
        project_id=project_id,
        folder_id=folder_id,
        name=name,
        description=description,
        version=version,
        version_group_id=version_group_id,
        file_path=file_path,
        size_bytes=size_bytes,
        uploader_id=current_user.id,
        ingest_source=source_type,
        version_notes=(version_notes or None),
    )
    db.add(db_video)
    db.flush()

    if base_video is not None:
        # A new cut resets the review state of the deliverable. Without this,
        # "the client approved v2" silently reads as "the current cut is
        # approved" once v3 lands.
        chain_ids = [
            row[0]
            for row in db.query(Video.id)
            .filter(
                Video.project_id == project_id,
                Video.version_group_id == version_group_id,
                Video.id != db_video.id,
            )
            .all()
        ]
        supersede_open_decisions(db, chain_ids, superseded_by_video_id=db_video.id)
        apply_video_status(
            db,
            db_video,
            STATUS_IN_REVIEW,
            actor_user_id=current_user.id,
            skip_transition_check=True,
        )
        # The editor's punch list follows the work rather than dying with the
        # version it was raised against.
        carry_forward_open_change_requests(db, base_video, db_video)

        # Tell the people invested in the previous cut that a new one exists —
        # everyone who commented on it or holds open work on it, plus the
        # project owner. Without this, reviewers found out by accident.
        recipients: set[int] = set()
        for (uid,) in (
            db.query(Comment.user_id)
            .filter(Comment.video_id == base_video.id, Comment.user_id.isnot(None))
            .distinct()
            .all()
        ):
            recipients.add(uid)
        for (uid,) in (
            db.query(Comment.assignee_user_id)
            .filter(
                Comment.video_id == base_video.id,
                Comment.assignee_user_id.isnot(None),
            )
            .distinct()
            .all()
        ):
            recipients.add(uid)
        project_row = db.query(Project).filter(Project.id == project_id).first()
        if project_row and project_row.creator_id:
            recipients.add(project_row.creator_id)
        recipients.discard(current_user.id)

        actor_name = current_user.name or current_user.email or "A teammate"
        emit_notifications_sync(
            db,
            [
                NotificationSpec(
                    user_id=uid,
                    type=TYPE_NEW_VERSION,
                    project_id=project_id,
                    video_id=db_video.id,
                    actor_user_id=current_user.id,
                    message=f"{actor_name} uploaded v{version} of {name}",
                )
                for uid in recipients
            ],
        )

    normalized_language = normalize_language(language)
    db_tr = VideoTranscription(video_id=db_video.id, status="pending", language=normalized_language)
    db.add(db_tr)
    log_activity(
        db,
        user_id=current_user.id,
        project_id=project_id,
        action=activity_action,
        meta={"video_name": name, "video_id": db_video.id},
    )
    project_row = db.query(Project).filter(Project.id == project_id).first()
    workspace_id = project_row.workspace_id if project_row else None
    project_video_count = db.query(Video.id).filter(Video.project_id == project_id).count()
    event_properties = {
        "feature_key": "media_import",
        "project_id": project_id,
        "video_id": db_video.id,
        "source_type": source_type,
        "is_new_version": base_video is not None,
        "version_number": version,
        "size_bytes": max(0, int(size_bytes or 0)),
        "result": "success",
    }
    emit(
        db,
        "upload_completed",
        user=current_user,
        workspace_id=workspace_id,
        properties=event_properties,
    )
    emit(
        db,
        "feature_completed",
        user=current_user,
        workspace_id=workspace_id,
        properties={**event_properties, "completion_type": "media_registered"},
    )
    if source_type == "google_drive" and integration_import_id is not None:
        emit_once(
            db,
            "feature_result_used",
            event_id=f"feature:google-drive:import:{integration_import_id}:attached",
            user=current_user,
            workspace_id=workspace_id,
            properties={
                "feature_key": "google_drive",
                "project_id": project_id,
                "video_id": db_video.id,
                "import_id": integration_import_id,
                "result_action": "imported_media_attached",
                "result": "success",
            },
        )
    if project_video_count == 1:
        emit(
            db,
            "project_setup_started",
            user=current_user,
            workspace_id=workspace_id,
            properties={
                "project_id": project_id,
                "video_id": db_video.id,
                "source_type": source_type,
                "result": "success",
            },
        )
    db.commit()

    try:
        from app.jobs.queue import enqueue_transcription_job

        queued_job_id = enqueue_transcription_job(db_video.id, language=normalized_language)
        if queued_job_id:
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

    # Poster thumbnail (ffmpeg frame -> storage). Best-effort; replaces the
    # Cloudinary-URL-derived thumbnail for R2/local backends.
    try:
        from app.jobs.queue import enqueue_video_thumbnail_job

        enqueue_video_thumbnail_job(db_video.id)
    except Exception as e:
        logger.warning("Thumbnail job not enqueued for video %s: %s", db_video.id, e)

    # Auto-generate review proxy if enabled
    try:
        from app.services.proxy_service import auto_proxy_on_upload
        auto_proxy_on_upload(db, db_video.id)
    except Exception as e:
        logger.warning("Auto-proxy not triggered for video %s: %s", db_video.id, e)

    return db_video


@router.post("/", response_model=VideoDetailResponse)
def upload_video(
    project_id: int,
    video_file: UploadFile = File(...),
    name: str = Form(...),
    description: Optional[str] = Form(None),
    folder_id: Optional[int] = Form(None),
    # When set, this upload becomes the next version in that video's chain.
    version_of: Optional[int] = Form(None),
    # "What changed in this version" — shown to reviewers above the comment
    # feed so they can decide what to re-watch instead of starting over.
    version_notes: Optional[str] = Form(None),
    # Spoken language for transcription, ISO 639-1 (e.g. "en"). "auto"/""/absent = auto-detect.
    language: Optional[str] = Form(None),
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
        # Shared with register_video_version (app/services/video_versions.py)
        # so there is one implementation of the group/version math.
        version_group_id, version = resolve_version_chain(db, base_video)
    else:
        version_group_id = _uuid.uuid4().hex
        version = 1

    db_video = _finalize_project_video(
        db,
        project_id=project_id,
        current_user=current_user,
        file_path=file_url,
        size_bytes=uploaded_size,
        name=name,
        description=description,
        folder_id=folder_id,
        version=version,
        version_group_id=version_group_id,
        language=language,
        activity_action="video_uploaded",
        source_type="direct_upload",
        version_notes=version_notes,
        base_video=base_video,
    )

    db_video = (
        db.query(Video)
        .options(
            joinedload(Video.uploader),
            joinedload(Video.transcription),
        )
        .filter(Video.id == db_video.id)
        .first()
    )
    return _video_detail(
        db_video, current_user.id, db=db, db_project=db_project
    )


@router.post("/youtube", response_model=VideoDetailResponse)
def create_video_from_youtube(
    project_id: int,
    body: YoutubeVideoCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a project source video by resolving a YouTube URL directly
    (no repurpose job involved) — used by the create-project wizard when the
    user pastes a link instead of uploading a file."""
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_access_project(db, current_user.id, db_project):
        raise HTTPException(status_code=403, detail="Not authorized to upload videos to this project")
    assert_write_project_content(db, current_user, db_project)

    url = (body.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url is required")

    try:
        db_video = create_youtube_source_video(
            db,
            user=current_user,
            project_id=project_id,
            youtube_url=url,
            name=body.name,
            language=body.language,
            enqueue_transcription=True,
        )
    except YoutubeStreamResolveError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    log_activity(
        db,
        user_id=current_user.id,
        project_id=project_id,
        action="video_uploaded",
        meta={"video_name": db_video.name, "video_id": db_video.id, "source": "youtube_url"},
    )
    emit(
        db,
        "upload_completed",
        user=current_user,
        workspace_id=db_project.workspace_id,
        properties={
            "feature_key": "media_import",
            "project_id": project_id,
            "video_id": db_video.id,
            "source_type": "youtube",
            "is_new_version": False,
            "result": "success",
        },
    )
    emit(
        db,
        "feature_completed",
        user=current_user,
        workspace_id=db_project.workspace_id,
        properties={
            "feature_key": "media_import",
            "project_id": project_id,
            "video_id": db_video.id,
            "source_type": "youtube",
            "completion_type": "media_registered",
            "result": "success",
        },
    )
    db.commit()

    db_video = (
        db.query(Video)
        .options(
            joinedload(Video.uploader),
            joinedload(Video.transcription),
        )
        .filter(Video.id == db_video.id)
        .first()
    )
    return _video_detail(
        db_video, current_user.id, db=db, db_project=db_project
    )


@router.post("/from-upload", response_model=VideoDetailResponse)
def register_uploaded_video(
    project_id: int,
    body: VideoFromUploadCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Attach an already-uploaded file (from stateless POST /upload/video) as a
    project video — used by the create-project wizard, which uploads the file
    while the user is still stepping through the wizard, then registers it
    here at submit time."""
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_access_project(db, current_user.id, db_project):
        raise HTTPException(status_code=403, detail="Not authorized to upload videos to this project")
    assert_write_project_content(db, current_user, db_project)

    file_path = (body.file_path or "").strip()
    if not file_path:
        raise HTTPException(status_code=400, detail="file_path is required")

    source_type = "presigned_upload"
    integration_import_id = None
    if body.drive_import_id is not None:
        drive_import = (
            db.query(DriveImport)
            .filter(
                DriveImport.id == body.drive_import_id,
                DriveImport.user_id == current_user.id,
            )
            .first()
        )
        if drive_import is None:
            raise HTTPException(status_code=404, detail="Drive import not found")
        if drive_import.status != "completed" or not drive_import.file_path:
            raise HTTPException(status_code=409, detail="Drive import is not complete")
        if drive_import.file_path != file_path:
            raise HTTPException(status_code=400, detail="Drive import does not match file_path")
        source_type = "google_drive"
        integration_import_id = drive_import.id

    if body.folder_id is not None:
        folder = db.query(Folder).filter(Folder.id == body.folder_id, Folder.project_id == project_id).first()
        if not folder:
            raise HTTPException(status_code=404, detail="Folder not found in this project")

    incoming_size = int(body.size_bytes or 0)
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

    import uuid as _uuid

    # Same chain semantics as the multipart route's `version_of`: inherit the
    # group and folder, and let _finalize_project_video run carry-forward,
    # approval superseding, and new-version notifications.
    base_video: Optional[Video] = None
    folder_id = body.folder_id
    if body.version_of is not None:
        base_video = (
            db.query(Video)
            .filter(Video.id == body.version_of, Video.project_id == project_id)
            .first()
        )
        if not base_video:
            raise HTTPException(
                status_code=404, detail="Version target video not found in this project"
            )
        folder_id = base_video.folder_id

    if base_video is not None:
        version_group_id, version = resolve_version_chain(db, base_video)
    else:
        version_group_id = _uuid.uuid4().hex
        version = 1

    db_video = _finalize_project_video(
        db,
        project_id=project_id,
        current_user=current_user,
        file_path=file_path,
        size_bytes=incoming_size,
        name=body.name,
        description=body.description,
        folder_id=folder_id,
        version=version,
        version_group_id=version_group_id,
        language=body.language,
        activity_action="video_uploaded",
        source_type=source_type,
        integration_import_id=integration_import_id,
        version_notes=body.version_notes,
        base_video=base_video,
    )

    db_video = (
        db.query(Video)
        .options(
            joinedload(Video.uploader),
            joinedload(Video.transcription),
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
        )
        .filter(Video.id == video_id, Video.project_id == project_id)
        .first()
    )
    if not db_video:
        raise HTTPException(status_code=404, detail="Video not found")
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not can_access_project(db, current_user.id, db_project):
        raise HTTPException(status_code=403, detail="Not authorized to access this video")

    detail = _video_detail(
        db_video, current_user.id, db=db, db_project=db_project
    )
    detail["project"] = db_project
    # All versions in this video's chain (same version_group_id), newest first.
    detail["versions"] = video_versions_payload(db, db_video)
    record_ingested_video_result_use(
        video=db_video,
        user_id=current_user.id,
        workspace_id=db_project.workspace_id if db_project else None,
    )
    return detail


@router.post("/{video_id}/transcription", response_model=VideoWithProjectResponse)
def start_project_video_transcription(
    project_id: int,
    video_id: int,
    force: bool = Query(
        False,
        description="If true, reset stuck queued/processing and enqueue again.",
    ),
    language: Optional[str] = Query(
        None,
        description="ISO 639-1 spoken language for this run (e.g. 'en'). Omit to keep the "
        "video's existing language selection; 'auto'/'' resets to auto-detect.",
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

    prepare_and_enqueue_transcription(db, video_id, force=force, language=language)

    db_video = (
        db.query(Video)
        .options(
            joinedload(Video.uploader),
            joinedload(Video.transcription),
        )
        .filter(Video.id == video_id, Video.project_id == project_id)
        .first()
    )
    detail = _video_detail(
        db_video, current_user.id, db=db, db_project=db_project
    )
    detail["project"] = db_project
    detail["versions"] = video_versions_payload(db, db_video)
    return detail


@router.get("/{video_id}/next-version-preview")
def next_version_preview(
    project_id: int,
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """What uploading a new version of this cut will do.

    The upload dialog states the consequence before the editor commits to it
    ("4 open change requests will move to v3") rather than surprising them
    afterwards.
    """
    db_video = (
        db.query(Video)
        .filter(Video.id == video_id, Video.project_id == project_id)
        .first()
    )
    if not db_video:
        raise HTTPException(status_code=404, detail="Video not found")
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not can_access_project(db, current_user.id, db_project):
        raise HTTPException(status_code=403, detail="Not authorized")

    return {
        "current_version": db_video.version or 1,
        "next_version": (db_video.version or 1) + 1,
        "carry_forward_count": count_open_change_requests(db, db_video.id),
        "suggested_name": db_video.name,
    }


@router.put("/{video_id}/status", response_model=VideoDetailResponse)
def update_video_status(
    project_id: int,
    video_id: int,
    data: VideoStatusUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db_video = (
        db.query(Video)
        .options(
            joinedload(Video.uploader),
            joinedload(Video.transcription),
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

    # The vocabulary, the legal moves, and the provenance all live in the
    # service — this handler and its twin in video_detail.py used to carry
    # their own copies of the status tuple and silently allowed any move.
    try:
        if data.status == STATUS_APPROVED:
            record_decision(
                db,
                db_video,
                DECISION_APPROVED,
                actor_user_id=current_user.id,
                skip_transition_check=False,
            )
        else:
            apply_video_status(db, db_video, data.status, actor_user_id=current_user.id)
    except (InvalidVideoStatus, IllegalStatusTransition) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db.commit()
    db.refresh(db_video)
    db_video = (
        db.query(Video)
        .options(
            joinedload(Video.uploader),
            joinedload(Video.transcription),
        )
        .filter(Video.id == video_id, Video.project_id == project_id)
        .first()
    )
    return _video_detail(
        db_video, current_user.id, db=db, db_project=db_project
    )


@router.post("/{video_id}/send-for-review", response_model=VideoDetailResponse)
async def send_video_for_review(
    project_id: int,
    video_id: int,
    data: SendForReviewRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Put a cut in front of reviewers.

    Deliberately separate from the generic status endpoint: sending has
    consequences — a deadline is recorded and people are notified — that a raw
    status write must never trigger silently.
    """
    db_video = (
        db.query(Video)
        .options(
            joinedload(Video.uploader),
            joinedload(Video.transcription),
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

    try:
        apply_video_status(
            db,
            db_video,
            STATUS_IN_REVIEW,
            actor_user_id=current_user.id,
            note=data.note,
        )
    except (InvalidVideoStatus, IllegalStatusTransition) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db_video.review_due_at = data.due_at
    db.commit()
    db.refresh(db_video)

    # Reviewers named explicitly, else whoever owns the work.
    recipients = set(data.reviewer_user_ids or [])
    if not recipients:
        recipients = {db_project.creator_id, db_video.uploader_id}
    recipients.discard(None)
    recipients.discard(current_user.id)

    actor_name = current_user.name or current_user.email or "A teammate"
    await emit_notifications(
        db,
        [
            NotificationSpec(
                user_id=uid,
                type=TYPE_REVIEW_REQUESTED,
                project_id=project_id,
                video_id=video_id,
                actor_user_id=current_user.id,
                message=f"{actor_name} asked you to review {db_video.name}",
            )
            for uid in recipients
        ],
    )

    return _video_detail(db_video, current_user.id, db=db, db_project=db_project)
