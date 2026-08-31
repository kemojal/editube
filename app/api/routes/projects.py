from __future__ import annotations

import logging
from typing import List, NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.api.models.projects import (
    CollaboratorEmailList,
    LibraryVideoResponse,
    ProjectCollaboratorUpdate,
    ProjectCreate,
    ProjectResponse,
    ProjectUpdate,
    UserResponse,
    WorkspaceAssetLinkCreate,
    WorkspaceAssetLinkResponse,
)
from app.db.database import get_db
from app.db.models import (
    AiResult,
    Clip,
    Folder,
    Project,
    ProjectCollaborator,
    ProjectTemplate,
    ProjectWorkspaceAssetLink,
    ProjectArchiveState,
    ProjectRetentionPolicy,
    Notification,
    RepurposeJob,
    User,
    Video,
    VideoTranscription,
    WorkspaceAsset,
    WorkspaceMember,
)
from app.jobs.queue import enqueue_push_notification_job
from app.jobs.queue import enqueue_archive_cold_storage_job
from app.services.project_access import (
    assert_write_project_content,
    can_access_project,
    can_manage_project_settings,
    get_project_for_user,
    get_workspace_member,
    list_users_for_mentions,
)
from app.services.rough_cut_workspace import (
    ROUGH_CUT_ASSET_DESCRIPTION,
    latest_project_source_video,
)
from app.services.mentions import user_mention_handles
from app.services.project_template_apply import apply_project_template
from app.services.workspace_bootstrap import ensure_personal_workspace
from app.services.product_analytics import emit, emit_once
from app.services.activity import log_activity
from app.utils.email import send_invitation_email
from app.utils.security import get_current_user
from app.websocket_manager import notifications_ws_manager

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/projects",
    tags=["Projects"],
)

def _latest_project_source_video(db: Session, project_id: int) -> Video | None:
    """Compatibility wrapper for callers and focused selector tests."""

    return latest_project_source_video(db, project_id)


def _user_resp(u: User) -> UserResponse:
    return UserResponse(
        id=u.id,
        name=u.full_name or u.name or u.email,
        email=u.email,
        avatar_url=getattr(u, "avatar_url", None),
        created_at=u.created_at.isoformat(),
        updated_at=u.updated_at.isoformat(),
    )


def _workspace_members_by_workspace(
    db: Session, workspace_ids: list[int]
) -> dict[int, list[User]]:
    if not workspace_ids:
        return {}
    rows = (
        db.query(WorkspaceMember.workspace_id, User)
        .join(User, User.id == WorkspaceMember.user_id)
        .filter(WorkspaceMember.workspace_id.in_(workspace_ids))
        .all()
    )
    members: dict[int, list[User]] = {}
    for workspace_id, user in rows:
        members.setdefault(workspace_id, []).append(user)
    return members


class _ProjectRollup(NamedTuple):
    thumbnail_url: str | None
    latest_video_id: int
    latest_version: int | None
    latest_version_count: int
    video_count: int


def _video_rollups_by_project(
    db: Session, project_ids: list[int]
) -> dict[int, _ProjectRollup]:
    """Batched latest_project_source_video plus per-project content counts.

    One window query over non-asset videos yields, per project: the latest
    source video (same ordering as latest_project_source_video), its version,
    the size of its version chain, and the total video count.
    """
    if not project_ids:
        return {}
    normalized_description = func.lower(func.trim(func.coalesce(Video.description, "")))
    # Ungrouped videos are each their own version chain.
    chain_key = func.coalesce(
        Video.version_group_id, func.concat("video-", cast(Video.id, String))
    )
    row_number = (
        func.row_number()
        .over(
            partition_by=Video.project_id,
            order_by=(Video.updated_at.desc(), Video.id.desc()),
        )
        .label("row_number")
    )
    chain_size = (
        func.count()
        .over(partition_by=(Video.project_id, chain_key))
        .label("chain_size")
    )
    video_count = (
        func.count().over(partition_by=Video.project_id).label("video_count")
    )
    ranked = (
        select(
            Video.id,
            Video.project_id,
            Video.thumbnail_url,
            Video.version,
            row_number,
            chain_size,
            video_count,
        )
        .where(Video.project_id.in_(project_ids))
        .where(normalized_description != ROUGH_CUT_ASSET_DESCRIPTION)
        .subquery()
    )
    rows = db.execute(
        select(
            ranked.c.id,
            ranked.c.project_id,
            ranked.c.thumbnail_url,
            ranked.c.version,
            ranked.c.chain_size,
            ranked.c.video_count,
        ).where(ranked.c.row_number == 1)
    ).all()
    return {
        row.project_id: _ProjectRollup(
            thumbnail_url=row.thumbnail_url,
            latest_video_id=row.id,
            latest_version=row.version,
            latest_version_count=row.chain_size,
            video_count=row.video_count,
        )
        for row in rows
    }


def _folder_counts_by_project(db: Session, project_ids: list[int]) -> dict[int, int]:
    if not project_ids:
        return {}
    rows = (
        db.query(Folder.project_id, func.count(Folder.id))
        .filter(Folder.project_id.in_(project_ids))
        .group_by(Folder.project_id)
        .all()
    )
    return dict(rows)


def convert_projects_to_responses(
    db: Session | None, projects: list[Project]
) -> list[ProjectResponse]:
    ws_members_by_id: dict[int, list[User]] = {}
    rollups_by_project: dict[int, _ProjectRollup] = {}
    folder_counts: dict[int, int] = {}
    if db and projects:
        workspace_ids = sorted({p.workspace_id for p in projects if p.workspace_id})
        project_ids = [p.id for p in projects]
        ws_members_by_id = _workspace_members_by_workspace(db, workspace_ids)
        rollups_by_project = _video_rollups_by_project(db, project_ids)
        folder_counts = _folder_counts_by_project(db, project_ids)

    responses: list[ProjectResponse] = []
    for db_project in projects:
        # Start with project-level collaborators
        seen: set[int] = {db_project.creator.id}
        members: list[UserResponse] = []
        for pc in db_project.collaborators:
            if pc.user.id not in seen:
                seen.add(pc.user.id)
                members.append(_user_resp(pc.user))

        # Also include workspace members so the avatar stack shows everyone
        if db and db_project.workspace_id:
            for u in ws_members_by_id.get(db_project.workspace_id, []):
                if u.id not in seen:
                    seen.add(u.id)
                    members.append(_user_resp(u))

        rollup = rollups_by_project.get(db_project.id)

        responses.append(
            ProjectResponse(
                id=db_project.id,
                name=db_project.name,
                description=db_project.description,
                workspace_id=db_project.workspace_id,
                project_type=db_project.project_type,
                created_at=db_project.created_at.isoformat(),
                updated_at=db_project.updated_at.isoformat(),
                creator=_user_resp(db_project.creator),
                collaborators=members,
                thumbnail_url=rollup.thumbnail_url if rollup else None,
                latest_video_id=rollup.latest_video_id if rollup else None,
                video_count=(rollup.video_count if rollup else 0) if db else None,
                folder_count=folder_counts.get(db_project.id, 0) if db else None,
                latest_version=rollup.latest_version if rollup else None,
                latest_version_count=rollup.latest_version_count if rollup else None,
            )
        )
    return responses


def convert_project_to_response(db_project: Project, db: Session | None = None) -> ProjectResponse:
    return convert_projects_to_responses(db, [db_project])[0]


def _ensure_workspace_collaborator(db: Session, project: Project, user_id: int) -> None:
    if not project.workspace_id:
        return
    exists = (
        db.query(WorkspaceMember)
        .filter(WorkspaceMember.workspace_id == project.workspace_id, WorkspaceMember.user_id == user_id)
        .first()
    )
    if exists:
        return
    db.add(
        WorkspaceMember(
            workspace_id=project.workspace_id,
            user_id=user_id,
            role="editor",
        )
    )


def _project_invite_message(project: Project, inviter: User) -> str:
    inviter_name = inviter.full_name or inviter.name or inviter.email or "A teammate"
    return f"{inviter_name} invited you to collaborate on {project.name}"


async def _emit_project_invite_notification(
    db: Session,
    *,
    project: Project,
    inviter: User,
    invited_user: User | None,
) -> None:
    if invited_user is None:
        return
    msg = _project_invite_message(project, inviter)
    existing_unread = (
        db.query(Notification)
        .filter(
            Notification.user_id == invited_user.id,
            Notification.type == "project_invite",
            Notification.project_id == project.id,
            Notification.read.is_(False),
        )
        .first()
    )
    if existing_unread:
        existing_unread.message = msg
        db.commit()
        db.refresh(existing_unread)
        enqueue_push_notification_job(existing_unread.user_id, existing_unread.id)
        await notifications_ws_manager.send_to_user(
            existing_unread.user_id,
            {
                "event": "notification.new",
                "payload": {
                    "id": existing_unread.id,
                    "type": existing_unread.type,
                    "read": existing_unread.read,
                    "project_id": existing_unread.project_id,
                    "video_id": existing_unread.video_id,
                    "comment_id": existing_unread.comment_id,
                    "workspace_id": existing_unread.workspace_id,
                    "workspace_invite_id": existing_unread.workspace_invite_id,
                    "invite_token": existing_unread.invite_token,
                    "message": existing_unread.message,
                    "created_at": existing_unread.created_at.isoformat() if existing_unread.created_at else None,
                },
            },
        )
        return
    notification = Notification(
        user_id=invited_user.id,
        type="project_invite",
        project_id=project.id,
        workspace_id=project.workspace_id,
        message=msg,
        read=False,
    )
    db.add(notification)
    db.commit()
    db.refresh(notification)
    enqueue_push_notification_job(notification.user_id, notification.id)
    await notifications_ws_manager.send_to_user(
        notification.user_id,
        {
            "event": "notification.new",
            "payload": {
                "id": notification.id,
                "type": notification.type,
                "read": notification.read,
                "project_id": notification.project_id,
                "video_id": notification.video_id,
                "comment_id": notification.comment_id,
                "workspace_id": notification.workspace_id,
                "workspace_invite_id": notification.workspace_invite_id,
                "invite_token": notification.invite_token,
                "message": notification.message,
                "created_at": notification.created_at.isoformat() if notification.created_at else None,
            },
        },
    )


@router.post("/", response_model=ProjectResponse)
def create_project(
    project: ProjectCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws_id = project.workspace_id
    if ws_id is not None:
        wm = get_workspace_member(db, ws_id, current_user.id)
        if not wm:
            raise HTTPException(status_code=403, detail="Not a member of that workspace")
        if wm.role in ("client", "guest"):
            raise HTTPException(
                status_code=403,
                detail="Not allowed to create projects in this workspace with your role",
            )
    else:
        ws = ensure_personal_workspace(db, current_user)
        ws_id = ws.id

    db_project = Project(
        name=project.name,
        description=project.description,
        creator_id=current_user.id,
        workspace_id=ws_id,
        project_type=project.project_type,
    )
    db.add(db_project)
    db.flush()

    if project.template_key:
        tpl = (
            db.query(ProjectTemplate)
            .filter(
                ProjectTemplate.template_key == project.template_key,
                ProjectTemplate.workspace_id == ws_id,
            )
            .first()
        ) or (
            db.query(ProjectTemplate)
            .filter(
                ProjectTemplate.template_key == project.template_key,
                ProjectTemplate.workspace_id.is_(None),
            )
            .first()
        )
        if tpl:
            apply_project_template(db, db_project, tpl, current_user.id)
            db_project.created_from_template_id = tpl.id

    log_activity(db, user_id=current_user.id, project_id=db_project.id, action="project_created")
    emit(
        db,
        "project_created",
        user=current_user,
        workspace_id=ws_id,
        properties={
            "project_id": db_project.id,
            "project_type": project.project_type,
            "template_used": bool(project.template_key),
            "result": "success",
        },
    )
    db.commit()
    db.refresh(db_project)
    return convert_project_to_response(db_project, db)


@router.get("/", response_model=List[ProjectResponse])
def get_user_projects(
    project_type: str | None = None,
    limit: int | None = Query(default=None, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    collaborating = select(ProjectCollaborator.project_id).where(
        ProjectCollaborator.user_id == current_user.id
    )
    member_workspaces = select(WorkspaceMember.workspace_id).where(
        WorkspaceMember.user_id == current_user.id,
        WorkspaceMember.role != "client",
    )
    query = (
        db.query(Project)
        .filter(
            or_(
                Project.creator_id == current_user.id,
                Project.id.in_(collaborating),
                Project.workspace_id.in_(member_workspaces),
            )
        )
        .options(
            joinedload(Project.creator),
            selectinload(Project.collaborators).joinedload(ProjectCollaborator.user),
        )
        .order_by(Project.updated_at.desc(), Project.id.desc())
    )
    if project_type:
        query = query.filter(Project.project_type == project_type)
    if offset:
        query = query.offset(offset)
    if limit is not None:
        query = query.limit(limit)
    return convert_projects_to_responses(db, query.all())


@router.get("/library-videos", response_model=List[LibraryVideoResponse])
def get_library_videos(
    limit: int = Query(default=500, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Every video the user can reach, newest first, in one query.

    Replaces the editor's recursive per-project, per-folder contents crawl
    (one request per folder) with a single request for the media library.
    """
    collaborating = select(ProjectCollaborator.project_id).where(
        ProjectCollaborator.user_id == current_user.id
    )
    member_workspaces = select(WorkspaceMember.workspace_id).where(
        WorkspaceMember.user_id == current_user.id,
        WorkspaceMember.role != "client",
    )
    rows = (
        db.query(Video, Project.name)
        .join(Project, Project.id == Video.project_id)
        .filter(
            or_(
                Project.creator_id == current_user.id,
                Project.id.in_(collaborating),
                Project.workspace_id.in_(member_workspaces),
            )
        )
        .order_by(Video.updated_at.desc(), Video.id.desc())
        .limit(limit)
        .all()
    )
    return [
        LibraryVideoResponse(
            id=video.id,
            name=video.name,
            description=video.description,
            version=video.version or 1,
            version_group_id=video.version_group_id,
            file_path=video.file_path,
            thumbnail_url=video.thumbnail_url,
            project_id=video.project_id,
            project_name=project_name or "",
            folder_id=video.folder_id,
            uploader_id=video.uploader_id,
            created_at=video.created_at,
            updated_at=video.updated_at,
        )
        for video, project_name in rows
    ]


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(project_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    try:
        db_project = db.query(Project).filter(Project.id == project_id).first()
        if not db_project:
            raise HTTPException(status_code=404, detail="Project not found")
        if not can_access_project(db, current_user.id, db_project):
            raise HTTPException(status_code=403, detail="Not authorized to access this project")
        return convert_project_to_response(db_project, db)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error retrieving project %s", project_id)
        raise HTTPException(status_code=500, detail="Internal server error") from e


def _pipeline_status_for_video(db: Session, video: Video) -> dict:
    """Read-only pipeline status for one video: transcription / analysis /
    clips states, derived from cheap existence + status queries. No writes,
    no heavy computation — safe to poll frequently from the studio shell."""
    vt = db.query(VideoTranscription).filter(VideoTranscription.video_id == video.id).first()
    transcription_state = vt.status if vt else "none"

    prefs_row = (
        db.query(AiResult)
        .filter(AiResult.video_id == video.id, AiResult.result_type == "auto_edit_prefs")
        .first()
    )
    auto_edit_enabled = bool(
        isinstance(prefs_row.result_data if prefs_row else None, dict)
        and prefs_row.result_data.get("enabled")
    )

    draft_row = (
        db.query(AiResult)
        .filter(AiResult.video_id == video.id, AiResult.result_type == "rough_cut_draft")
        .first()
    )
    ai_analysis = (
        draft_row.result_data.get("aiAnalysis")
        if draft_row and isinstance(draft_row.result_data, dict)
        else None
    )
    # Only the server-side auto-edit hook (`run_post_transcription_auto_edit`)
    # stamps `analyzedAt: "transcription:<id>"` on aiAnalysis; a client-format
    # draft save (rough-cut-draft-state.ts's `aiAnalysis: {showFillers,
    # removeSilence, smoothSpeech, suggestions}`, no `analyzedAt`) must never
    # read as "done" here.
    analyzed_at = ai_analysis.get("analyzedAt") if isinstance(ai_analysis, dict) else None
    has_analysis = isinstance(analyzed_at, str) and analyzed_at.startswith("transcription:")
    if has_analysis:
        analysis_state = "done"
    elif auto_edit_enabled:
        analysis_state = "pending"
    else:
        analysis_state = "none"
    # A failed transcription will never produce analysis — don't leave the
    # strip spinning on "pending" (and therefore polling) forever. A
    # previously-completed analysis (e.g. from an earlier successful run,
    # before a later retry failed) stays "done" rather than being erased.
    if transcription_state == "failed" and analysis_state != "done":
        analysis_state = "none"

    jobs_exist = (
        db.query(RepurposeJob.id).filter(RepurposeJob.video_id == video.id).first() is not None
    )
    if not jobs_exist:
        clips_state, ready, total = "none", 0, 0
    else:
        total = db.query(Clip).filter(Clip.video_id == video.id).count()
        ready = db.query(Clip).filter(Clip.video_id == video.id, Clip.status == "ready").count()
        if total == 0:
            clips_state = "waiting"
        elif ready < total:
            clips_state = "generating"
        else:
            clips_state = "done"

    return {
        "video_id": video.id,
        "transcription": transcription_state,
        "analysis": analysis_state,
        "clips": {"state": clips_state, "ready": ready, "total": total},
    }


@router.get("/{project_id}/mentionable-users")
def list_mentionable_users(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Everyone who can be @mentioned on this project.

    The composer used to build its own candidate list from the uploader plus
    whoever had already commented, so a teammate who had not yet said anything
    was unmentionable — including, often, the very person you needed to pull
    in. The backend has always known the real roster; it just had no way to
    hand it over.

    Each entry carries the handles the resolver will actually match, so the
    picker cannot offer a name that then fails to notify anyone. Clients are
    excluded: they participate through guest review links, not @mentions.
    """
    project = get_project_for_user(db, project_id, current_user)
    users = list_users_for_mentions(db, project)
    return [
        {
            "id": user.id,
            "name": user.name or user.email,
            "email": user.email,
            "avatar_url": user.avatar_url,
            # Sorted so the primary handle is stable between renders.
            "handles": sorted(user_mention_handles(user)),
            "is_you": user.id == current_user.id,
        }
        for user in users
    ]


@router.get("/{project_id}/pipeline")
def get_project_pipeline(
    project_id: int,
    video_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Studio pipeline status strip: transcription -> analysis -> clips, for
    one video in the project (default: the project's most recently updated
    video). Read-only, cheap queries only."""
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_access_project(db, current_user.id, db_project):
        raise HTTPException(status_code=403, detail="Not authorized to access this project")

    if video_id is not None:
        video = (
            db.query(Video)
            .filter(Video.id == video_id, Video.project_id == project_id)
            .first()
        )
        if not video:
            raise HTTPException(status_code=404, detail="Video not found in project")
    else:
        video = (
            db.query(Video)
            .filter(Video.project_id == project_id)
            .order_by(Video.updated_at.desc())
            .first()
        )

    if video is None:
        return {
            "video_id": None,
            "transcription": "none",
            "analysis": "none",
            "clips": {"state": "none", "ready": 0, "total": 0},
        }

    return _pipeline_status_for_video(db, video)


@router.put("/{project_id}", response_model=ProjectResponse)
def update_project(
    project_id: int,
    project: ProjectUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_manage_project_settings(db, current_user.id, db_project):
        raise HTTPException(status_code=403, detail="Not authorized to update this project")
    update_data = project.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_project, key, value)
    db.commit()
    db.refresh(db_project)
    return convert_project_to_response(db_project, db)


@router.delete("/{project_id}")
def delete_project(project_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")
    if db_project.creator_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this project")
    db.delete(db_project)
    db.commit()
    return {"message": "Project deleted successfully"}


@router.post("/{project_id}/restore-from-cold")
def restore_from_cold(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_manage_project_settings(db, current_user.id, db_project):
        raise HTTPException(status_code=403, detail="Not authorized")
    state = db.query(ProjectArchiveState).filter(ProjectArchiveState.project_id == project_id).first()
    if not state:
        state = ProjectArchiveState(project_id=project_id, state="active")
    state.state = "active"
    state.archived_at = None
    state.cold_moved_at = None
    db.add(state)
    db.commit()
    return {"ok": True, "state": state.state}


@router.post("/{project_id}/archive-to-cold")
def archive_to_cold(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_manage_project_settings(db, current_user.id, db_project):
        raise HTTPException(status_code=403, detail="Not authorized")
    policy = db.query(ProjectRetentionPolicy).filter(ProjectRetentionPolicy.project_id == project_id).first()
    if not policy:
        policy = ProjectRetentionPolicy(project_id=project_id, auto_archive_enabled=True, archive_after_days=90)
    db.add(policy)
    db.commit()
    queued = enqueue_archive_cold_storage_job(project_id)
    return {"ok": True, "enqueued": queued}


@router.get("/{project_id}/retention-state")
def get_retention_state(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_manage_project_settings(db, current_user.id, db_project):
        raise HTTPException(status_code=403, detail="Not authorized")

    policy = db.query(ProjectRetentionPolicy).filter(ProjectRetentionPolicy.project_id == project_id).first()
    if not policy:
        policy = ProjectRetentionPolicy(
            project_id=project_id,
            auto_archive_enabled=True,
            archive_after_days=90,
        )
        db.add(policy)
        db.commit()
        db.refresh(policy)

    state = db.query(ProjectArchiveState).filter(ProjectArchiveState.project_id == project_id).first()
    if not state:
        state = ProjectArchiveState(project_id=project_id, state="active")
        db.add(state)
        db.commit()
        db.refresh(state)

    return {
        "ok": True,
        "policy": {
            "project_id": policy.project_id,
            "auto_archive_enabled": bool(policy.auto_archive_enabled),
            "archive_after_days": int(policy.archive_after_days or 90),
            "cold_tier_provider": policy.cold_tier_provider,
            "last_archive_run_at": policy.last_archive_run_at,
        },
        "state": {
            "project_id": state.project_id,
            "state": state.state,
            "archived_at": state.archived_at,
            "cold_moved_at": state.cold_moved_at,
            "storage_location_meta": state.storage_location_meta,
        },
    }


@router.patch("/{project_id}/retention-policy")
def update_retention_policy(
    project_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_manage_project_settings(db, current_user.id, db_project):
        raise HTTPException(status_code=403, detail="Not authorized")

    policy = db.query(ProjectRetentionPolicy).filter(ProjectRetentionPolicy.project_id == project_id).first()
    if not policy:
        policy = ProjectRetentionPolicy(project_id=project_id, auto_archive_enabled=True, archive_after_days=90)

    if "auto_archive_enabled" in body:
        policy.auto_archive_enabled = bool(body.get("auto_archive_enabled"))
    if "archive_after_days" in body:
        try:
            days = int(body.get("archive_after_days"))
            policy.archive_after_days = max(1, min(3650, days))
        except Exception:
            raise HTTPException(status_code=400, detail="archive_after_days must be an integer")
    if "cold_tier_provider" in body:
        v = body.get("cold_tier_provider")
        policy.cold_tier_provider = str(v).strip() if v else None

    db.add(policy)
    db.commit()
    db.refresh(policy)
    return {
        "ok": True,
        "policy": {
            "project_id": policy.project_id,
            "auto_archive_enabled": bool(policy.auto_archive_enabled),
            "archive_after_days": int(policy.archive_after_days or 90),
            "cold_tier_provider": policy.cold_tier_provider,
            "last_archive_run_at": policy.last_archive_run_at,
        },
    }


@router.post("/{project_id}/collaborators")
async def invite_collaborators(
    project_id: int,
    email_list: CollaboratorEmailList,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_manage_project_settings(db, current_user.id, db_project):
        raise HTTPException(status_code=403, detail="Not authorized to invite collaborators to this project")

    roles_map = {k.lower(): v for k, v in (email_list.collaborator_roles or {}).items()}
    new_collaborators = []
    for email in email_list.collaborator_emails:
        em = (email or "").strip()
        if not em:
            continue
        role = (roles_map.get(em.lower()) or "editor").strip() or "editor"
        collaborator = db.query(User).filter(func.lower(User.email) == em.lower()).first()
        if collaborator:
            if collaborator in [c.user for c in db_project.collaborators]:
                await _emit_project_invite_notification(
                    db,
                    project=db_project,
                    inviter=current_user,
                    invited_user=collaborator,
                )
                continue
            new_collaborator = ProjectCollaborator(
                project_id=project_id,
                user_id=collaborator.id,
                role=role,
            )
            db.add(new_collaborator)
            _ensure_workspace_collaborator(db, db_project, collaborator.id)
            new_collaborators.append(new_collaborator)
            db.flush()
            await _emit_project_invite_notification(
                db,
                project=db_project,
                inviter=current_user,
                invited_user=collaborator,
            )
        else:
            send_invitation_email(db, em, project_id)

    db.commit()
    db.refresh(db_project)
    return {"added": len(new_collaborators)}


@router.patch("/{project_id}/collaborators/{user_id}", response_model=ProjectResponse)
def update_collaborator_role(
    project_id: int,
    user_id: int,
    body: ProjectCollaboratorUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_manage_project_settings(db, current_user.id, db_project):
        raise HTTPException(status_code=403, detail="Not authorized")
    row = (
        db.query(ProjectCollaborator)
        .filter(ProjectCollaborator.project_id == project_id, ProjectCollaborator.user_id == user_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Collaborator not found for this project")
    row.role = body.role.strip() or row.role
    db.commit()
    db.refresh(db_project)
    return convert_project_to_response(db_project, db)


@router.delete("/{project_id}/collaborators/{user_id}", response_model=ProjectResponse)
def remove_collaborator(
    project_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_manage_project_settings(db, current_user.id, db_project):
        raise HTTPException(status_code=403, detail="Not authorized to remove collaborators from this project")
    collaborator = (
        db.query(ProjectCollaborator)
        .filter(ProjectCollaborator.project_id == project_id, ProjectCollaborator.user_id == user_id)
        .first()
    )
    if not collaborator:
        raise HTTPException(status_code=404, detail="Collaborator not found for this project")
    db.delete(collaborator)
    db.commit()
    db.refresh(db_project)
    return convert_project_to_response(db_project, db)


def _workspace_asset_link_dict(link: ProjectWorkspaceAssetLink) -> dict:
    a = link.workspace_asset
    return {
        "id": link.id,
        "project_id": link.project_id,
        "workspace_asset_id": link.workspace_asset_id,
        "folder_id": link.folder_id,
        "category": a.category if a else "",
        "title": a.title if a else "",
        "file_url": a.file_url if a else "",
        "created_at": link.created_at,
    }


@router.get("/{project_id}/workspace-assets", response_model=List[WorkspaceAssetLinkResponse])
def list_project_workspace_assets(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = get_project_for_user(db, project_id, current_user)
    rows = (
        db.query(ProjectWorkspaceAssetLink)
        .options(joinedload(ProjectWorkspaceAssetLink.workspace_asset))
        .filter(ProjectWorkspaceAssetLink.project_id == project.id)
        .order_by(ProjectWorkspaceAssetLink.created_at.desc())
        .all()
    )
    return [_workspace_asset_link_dict(r) for r in rows]


@router.post("/{project_id}/workspace-assets", response_model=WorkspaceAssetLinkResponse)
def attach_workspace_asset_to_project(
    project_id: int,
    body: WorkspaceAssetLinkCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db_project = get_project_for_user(db, project_id, current_user)
    assert_write_project_content(db, current_user, db_project)

    asset = (
        db.query(WorkspaceAsset)
        .filter(WorkspaceAsset.id == body.workspace_asset_id)
        .first()
    )
    if not asset or asset.workspace_id != db_project.workspace_id:
        raise HTTPException(status_code=404, detail="Workspace asset not found in this workspace")

    if body.folder_id is not None:
        folder = (
            db.query(Folder)
            .filter(Folder.id == body.folder_id, Folder.project_id == project_id)
            .first()
        )
        if not folder:
            raise HTTPException(status_code=404, detail="Folder not found in this project")
    else:
        folder = None

    existing = (
        db.query(ProjectWorkspaceAssetLink)
        .filter(
            ProjectWorkspaceAssetLink.project_id == project_id,
            ProjectWorkspaceAssetLink.workspace_asset_id == body.workspace_asset_id,
        )
        .first()
    )
    if existing:
        existing.folder_id = body.folder_id
        emit_once(
            db,
            "feature_result_used",
            event_id=f"feature:workspace-assets:link:{existing.id}:attached",
            user=current_user,
            workspace_id=db_project.workspace_id,
            properties={
                "feature_key": "workspace_assets",
                "project_id": db_project.id,
                "workspace_asset_id": body.workspace_asset_id,
                "asset_link_id": existing.id,
                "result_action": "attached_to_project",
                "result": "success",
            },
        )
        db.commit()
        existing = (
            db.query(ProjectWorkspaceAssetLink)
            .filter(ProjectWorkspaceAssetLink.id == existing.id)
            .options(joinedload(ProjectWorkspaceAssetLink.workspace_asset))
            .first()
        )
        return _workspace_asset_link_dict(existing)

    link = ProjectWorkspaceAssetLink(
        project_id=project_id,
        workspace_asset_id=body.workspace_asset_id,
        folder_id=body.folder_id,
        created_by_user_id=current_user.id,
    )
    db.add(link)
    db.flush()
    emit_once(
        db,
        "feature_result_used",
        event_id=f"feature:workspace-assets:link:{link.id}:attached",
        user=current_user,
        workspace_id=db_project.workspace_id,
        properties={
            "feature_key": "workspace_assets",
            "project_id": db_project.id,
            "workspace_asset_id": body.workspace_asset_id,
            "asset_link_id": link.id,
            "result_action": "attached_to_project",
            "result": "success",
        },
    )
    db.commit()
    db.refresh(link)
    link = (
        db.query(ProjectWorkspaceAssetLink)
        .filter(ProjectWorkspaceAssetLink.id == link.id)
        .options(joinedload(ProjectWorkspaceAssetLink.workspace_asset))
        .first()
    )
    return _workspace_asset_link_dict(link)


@router.delete("/{project_id}/workspace-assets/{link_id}")
def detach_workspace_asset_from_project(
    project_id: int,
    link_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db_project = get_project_for_user(db, project_id, current_user)
    assert_write_project_content(db, current_user, db_project)
    link = (
        db.query(ProjectWorkspaceAssetLink)
        .filter(
            ProjectWorkspaceAssetLink.id == link_id,
            ProjectWorkspaceAssetLink.project_id == project_id,
        )
        .first()
    )
    if not link:
        raise HTTPException(status_code=404, detail="Link not found")
    db.delete(link)
    db.commit()
    return {"ok": True}
