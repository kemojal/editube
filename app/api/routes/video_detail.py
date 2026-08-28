import time

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, joinedload, selectinload

from app.db.database import get_db
from app.db.models import Project, Video, User
from app.api.models.videos import (
    ReviewDecisionRequest,
    VideoDetailResponse,
    VideoStatusUpdate,
    VideoWithProjectResponse,
)
from app.api.video_payload import video_detail_dict, video_versions_payload
from app.utils.security import get_current_user
from app.services.comment_workflow import video_approve_blockers
from app.services.notifications import (
    TYPE_CHANGES_REQUESTED,
    TYPE_VIDEO_APPROVED,
    NotificationSpec,
    emit_notifications,
)
from app.services.transcription_enqueue import prepare_and_enqueue_transcription
from app.services.video_status import (
    DECISION_APPROVED,
    STATUS_APPROVED,
    IllegalStatusTransition,
    InvalidVideoStatus,
    apply_video_status,
    record_decision,
)
from app.services.word_alignment import realign_words_to_text
from app.services.ingest_service import record_ingested_video_result_use
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


class TranscriptSegmentEdit(BaseModel):
    """One corrected cue, addressed by its position in the segment list."""

    index: int = Field(ge=0)
    text: str = Field(max_length=5000)


class TranscriptSegmentsUpdate(BaseModel):
    edits: list[TranscriptSegmentEdit] = Field(min_length=1, max_length=200)


@router.patch("/{video_id}/transcription/segments", response_model=VideoWithProjectResponse)
def update_transcription_segments(
    video_id: int,
    body: TranscriptSegmentsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Correct the text of individual transcript cues.

    Only `text` is writable. Timings and speaker attribution come from the
    model and stay put — this exists so a human can fix a misheard word, not
    re-cut the transcript.

    Edits are addressed by index rather than replacing the whole array: two
    people correcting different lines would otherwise overwrite each other with
    whatever their tab last loaded.
    """
    db_video = (
        db.query(Video)
        .options(joinedload(Video.transcription))
        .filter(Video.id == video_id)
        .first()
    )
    if not db_video:
        raise HTTPException(status_code=404, detail="Video not found")
    db_project = db.query(Project).filter(Project.id == db_video.project_id).first()
    if not can_access_project(db, current_user.id, db_project):
        raise HTTPException(status_code=403, detail="Not authorized to access this video")
    assert_write_project_content(db, current_user, db_project)

    transcription = db_video.transcription
    if not transcription or not isinstance(transcription.segments, list):
        raise HTTPException(status_code=404, detail="This video has no transcript yet")

    # Copy, mutate, reassign: SQLAlchemy does not track in-place edits to a
    # JSONB column, so mutating the loaded list would never be written back.
    segments = [dict(segment) for segment in transcription.segments]

    for edit in body.edits:
        if edit.index >= len(segments):
            raise HTTPException(
                status_code=400,
                detail=f"Segment {edit.index} does not exist in this transcript",
            )
        segment = segments[edit.index]
        new_text = edit.text.strip()
        # Per-word timings were produced for the original wording. Realign
        # them to the corrected text: unchanged words keep their real ASR
        # timings, rewritten spans inherit the replaced words' time envelope.
        # Only when nothing usable survives is `words` dropped.
        try:
            seg_start = float(segment.get("start") or 0.0)
            seg_end = float(segment.get("end") or seg_start)
            realigned = realign_words_to_text(
                new_text,
                segment.get("words"),
                seg_start=seg_start,
                seg_end=seg_end,
            )
        except Exception:
            realigned = None
        segment["text"] = new_text
        if realigned:
            segment["words"] = realigned
        else:
            segment.pop("words", None)
        segment["edited"] = True

    transcription.segments = segments
    db.commit()

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

    record_ingested_video_result_use(
        video=db_video,
        user_id=current_user.id,
        workspace_id=db_project.workspace_id if db_project else None,
    )
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


def _load_video_for_write(video_id: int, db: Session, current_user: User):
    """Fetch a video with the player's eager loads, and assert the caller may
    change it. Shared by the status and decision handlers."""
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
    return db_video, db_project


@router.put("/{video_id}/status", response_model=VideoDetailResponse)
def update_video_status(
    video_id: int,
    data: VideoStatusUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Project-scoped twin lives in `app/api/routes/videos.py`.

    Both now delegate to the same service, so the two can no longer disagree
    about what a valid status is — which they did, via two hand-maintained
    copies of the same tuple. The body was also an untyped `dict` here.
    """
    db_video, db_project = _load_video_for_write(video_id, db, current_user)

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
    return _video_detail(db_video, current_user.id, db=db, db_project=db_project)


@router.post("/{video_id}/review-decision", response_model=VideoDetailResponse)
async def submit_review_decision(
    video_id: int,
    data: ReviewDecisionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Approve a cut, or send it back with changes.

    The team-side counterpart to `POST /review/{token}/approve`. Both write a
    `VideoApproval` row and move `Video.status` through the same service, which
    is what makes approval one mechanism rather than several.
    """
    db_video, db_project = _load_video_for_write(video_id, db, current_user)

    if data.decision == DECISION_APPROVED and not data.override_blockers:
        blockers = video_approve_blockers(db, db_video)
        if blockers:
            # 409, not 400: the request is well-formed, the world just isn't
            # ready for it — and the client can retry with override_blockers.
            raise HTTPException(
                status_code=409,
                detail={
                    "message": blockers[0].get("message", "Approval blocked"),
                    "blockers": blockers,
                },
            )

    record_decision(
        db,
        db_video,
        data.decision,
        actor_user_id=current_user.id,
        note=data.note,
    )
    db.commit()
    db.refresh(db_video)

    actor_name = current_user.name or current_user.email or "A teammate"
    recipients = {db_project.creator_id, db_video.uploader_id}
    recipients.discard(None)
    recipients.discard(current_user.id)
    approved = data.decision == DECISION_APPROVED
    await emit_notifications(
        db,
        [
            NotificationSpec(
                user_id=uid,
                type=TYPE_VIDEO_APPROVED if approved else TYPE_CHANGES_REQUESTED,
                project_id=db_project.id,
                video_id=db_video.id,
                actor_user_id=current_user.id,
                message=(
                    f"{actor_name} approved {db_video.name}"
                    if approved
                    else f"{actor_name} requested changes on {db_video.name}"
                ),
            )
            for uid in recipients
        ],
    )

    return _video_detail(db_video, current_user.id, db=db, db_project=db_project)
