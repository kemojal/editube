import io
import logging
import os
from collections import defaultdict
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, joinedload
from typing import List

from app.db.database import get_db
from app.db.models import Project, Video, Comment, CommentLike, Notification, User, UserSettings
from app.services.project_access import (
    assert_write_project_content,
    can_access_project,
    can_moderate_video_comments,
    list_users_for_mentions,
)
from app.services.comment_visibility import (
    COMMENT_VISIBILITY_AUTHOR_ONLY,
    COMMENT_VISIBILITY_PUBLIC,
    COMMENT_VISIBILITY_TEAM,
    normalize_visibility,
)
from app.api.models.comments import (
    CommentBulkAction,
    CommentCreate,
    CommentUpdate,
    CommentResponse,
    CommentWithRepliesResponse,
    CommentUserResponse,
)
from app.jobs.queue import enqueue_mention_email_job
from app.services.mentions import extract_mention_handles, resolve_mentioned_users
from app.services.comment_export import export_comments
from app.services.comment_workflow import (
    COMMENT_KIND_CHANGE_REQUEST,
    COMMENT_KIND_COMMENT,
    COMMENT_STATUS_REOPENED,
    COMMENT_STATUS_RESOLVED,
    apply_status,
    sync_is_resolved_from_status,
)
from app.utils.security import get_current_user
from app.websocket_manager import notifications_ws_manager

router = APIRouter(
    prefix="/projects/{project_id}/videos/{video_id}/comments",
    tags=["Comments"],
)

logger = logging.getLogger(__name__)


def _check_video_access(project_id: int, video_id: int, db: Session, current_user: User):
    db_video = db.query(Video).filter(Video.id == video_id, Video.project_id == project_id).first()
    if not db_video:
        raise HTTPException(status_code=404, detail="Video not found")
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_access_project(db, current_user.id, db_project):
        raise HTTPException(status_code=403, detail="Not authorized")
    return db_video, db_project


def _is_team_member(db: Session, db_project: Project, user_id: int) -> bool:
    """Teammates who may see internal threads, assign, bulk-edit — excludes client role."""
    return can_moderate_video_comments(db, db_project, user_id)


def _comment_visible_to_viewer(comment: Comment, viewer_id: int, is_team: bool) -> bool:
    v = normalize_visibility(getattr(comment, "visibility", None), comment.is_private)
    if v == COMMENT_VISIBILITY_PUBLIC:
        return True
    if v == COMMENT_VISIBILITY_TEAM:
        return is_team
    if v == COMMENT_VISIBILITY_AUTHOR_ONLY:
        return comment.user_id == viewer_id
    return True


def _reply_visible_to_viewer(reply: Comment, parent: Comment, viewer_id: int, is_team: bool) -> bool:
    pv = normalize_visibility(getattr(parent, "visibility", None), parent.is_private)
    if pv == COMMENT_VISIBILITY_TEAM and not is_team:
        return False
    v = normalize_visibility(getattr(reply, "visibility", None), reply.is_private)
    if v == COMMENT_VISIBILITY_PUBLIC:
        return True
    if v == COMMENT_VISIBILITY_TEAM:
        return is_team
    if v == COMMENT_VISIBILITY_AUTHOR_ONLY:
        if reply.user_id == viewer_id:
            return True
        return parent.user_id == viewer_id
    return True


def _comment_or_reply_visible(
    db: Session,
    db_project: Project,
    comment: Comment,
    viewer_id: int,
    parent: Comment | None = None,
) -> bool:
    is_team = _is_team_member(db, db_project, viewer_id)
    if comment.parent_id is None:
        return _comment_visible_to_viewer(comment, viewer_id, is_team)
    if parent is not None and parent.id == comment.parent_id:
        return _reply_visible_to_viewer(comment, parent, viewer_id, is_team)
    parent_row = (
        db.query(Comment)
        .filter(Comment.id == comment.parent_id, Comment.video_id == comment.video_id)
        .first()
    )
    if not parent_row:
        return False
    return _reply_visible_to_viewer(comment, parent_row, viewer_id, is_team)


def _assignee_payload(comment: Comment) -> dict | None:
    if not comment.assignee_user_id or not comment.assignee:
        return None
    u = comment.assignee
    return CommentUserResponse(
        id=u.id,
        name=u.name or (u.email or "User"),
        email=u.email or "",
        avatar_url=getattr(u, "avatar_url", None),
    ).dict()


def _comment_response(comment: Comment, current_user_id: int) -> dict:
    likes_count = len(comment.likes) if comment.likes else 0
    liked_by_me = any(like.user_id == current_user_id for like in (comment.likes or []))
    replies_count = len(comment.replies) if comment.replies else 0
    kind = getattr(comment, "kind", None) or COMMENT_KIND_COMMENT
    status = getattr(comment, "status", None) or "open"
    return {
        "id": comment.id,
        "video_id": comment.video_id,
        "parent_id": comment.parent_id,
        "text": comment.text,
        "timecode": comment.timecode,
        "end_timecode": comment.end_timecode,
        "drawing_data": comment.drawing_data,
        "is_resolved": comment.is_resolved,
        "is_private": comment.is_private,
        "visibility": normalize_visibility(getattr(comment, "visibility", None), comment.is_private),
        "due_at": comment.due_at,
        "kind": kind,
        "status": status,
        "assignee": _assignee_payload(comment),
        "user": comment.user if comment.user_id else None,
        "guest_name": comment.guest_name,
        "guest_email": comment.guest_email,
        "guest_avatar_url": getattr(comment, "guest_avatar_url", None),
        "review_link_id": comment.review_link_id,
        "likes_count": likes_count,
        "liked_by_me": liked_by_me,
        "replies_count": replies_count,
        "created_at": comment.created_at,
        "updated_at": comment.updated_at,
    }


def _build_comment_tree(
    rows: List[Comment],
    current_user: User,
    db: Session,
    db_project: Project,
) -> List[dict]:
    by_parent: dict[int | None, List[Comment]] = defaultdict(list)
    for c in rows:
        by_parent[c.parent_id].append(c)

    def build(parent_id: int | None, parent_comment: Comment | None) -> List[dict]:
        children = sorted(
            by_parent.get(parent_id, []),
            key=lambda x: (x.timecode or 0, x.created_at),
        )
        out: List[dict] = []
        is_team = _is_team_member(db, db_project, current_user.id)
        for c in children:
            if parent_id is None:
                ok = _comment_visible_to_viewer(c, current_user.id, is_team)
            else:
                assert parent_comment is not None
                ok = _reply_visible_to_viewer(c, parent_comment, current_user.id, is_team)
            if not ok:
                continue
            item = _comment_response(c, current_user.id)
            nested = build(c.id, c)
            item["replies"] = nested
            item["replies_count"] = len(nested)
            out.append(item)
        return out

    return build(None, None)


@router.post("/", response_model=CommentResponse)
async def add_comment(
    project_id: int,
    video_id: int,
    comment: CommentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db_video, db_project = _check_video_access(project_id, video_id, db, current_user)
    assert_write_project_content(db, current_user, db_project)

    is_team = _is_team_member(db, db_project, current_user.id)
    if comment.parent_id is not None:
        parent = db.query(Comment).filter(
            Comment.id == comment.parent_id, Comment.video_id == video_id
        ).first()
        if not parent or not _comment_visible_to_viewer(parent, current_user.id, is_team):
            raise HTTPException(status_code=404, detail="Parent comment not found")

    kind = comment.kind if comment.kind in (COMMENT_KIND_COMMENT, COMMENT_KIND_CHANGE_REQUEST) else COMMENT_KIND_COMMENT
    vis = comment.visibility
    v = normalize_visibility(vis, comment.is_private)
    if v in (COMMENT_VISIBILITY_TEAM, COMMENT_VISIBILITY_AUTHOR_ONLY) and not is_team:
        raise HTTPException(status_code=403, detail="Only team members can create internal comments")
    is_priv = v != COMMENT_VISIBILITY_PUBLIC
    db_comment = Comment(
        video_id=video_id,
        user_id=current_user.id,
        parent_id=comment.parent_id,
        text=comment.text,
        timecode=comment.timecode,
        end_timecode=comment.end_timecode,
        drawing_data=comment.drawing_data,
        is_private=is_priv,
        visibility=v,
        due_at=getattr(comment, "due_at", None),
        kind=kind,
        status="open",
    )
    sync_is_resolved_from_status(db_comment)
    db.add(db_comment)
    db.commit()
    db.refresh(db_comment)
    db_comment = (
        db.query(Comment)
        .filter(Comment.id == db_comment.id)
        .options(joinedload(Comment.likes), joinedload(Comment.user), joinedload(Comment.assignee))
        .first()
    )

    handles = extract_mention_handles(comment.text or "")
    if handles:
        project_users = list_users_for_mentions(db, db_project)
        recipients = resolve_mentioned_users(
            mention_handles=handles,
            candidate_users=project_users,
            actor_user_id=current_user.id,
        )
        frontend_base = os.getenv("FRONTEND_BASE_URL", "http://localhost:3000").rstrip("/")
        comment_url = (
            f"{frontend_base}/player/{video_id}?"
            + urlencode({"tab": "comments", "commentId": str(db_comment.id)})
        )

        created_notifications: list[Notification] = []
        for recipient in recipients:
            notification = Notification(
                user_id=recipient.id,
                type="mention",
                project_id=project_id,
                video_id=video_id,
                comment_id=db_comment.id,
                read=False,
            )
            db.add(
                notification
            )
            created_notifications.append(notification)
            settings = db.query(UserSettings).filter(UserSettings.user_id == recipient.id).first()
            if settings is not None and not settings.email_mentions:
                continue
            queued = enqueue_mention_email_job(
                recipient_user_id=recipient.id,
                actor_name=current_user.name or current_user.email or "A teammate",
                project_name=db_project.name,
                video_name=db_video.name,
                comment_text=db_comment.text or "",
                comment_url=comment_url,
            )
            if not queued:
                logger.warning("Mention email enqueue skipped/failed for user %s", recipient.id)

        db.commit()
        for notification in created_notifications:
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
                        "created_at": notification.created_at.isoformat() if notification.created_at else None,
                    },
                },
            )

    return _comment_response(db_comment, current_user.id)


@router.get("/export")
def export_comments_file(
    project_id: int,
    video_id: int,
    format: str = Query("csv", description="csv | pdf | edl | fcpxml | premiere"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db_video, _ = _check_video_access(project_id, video_id, db, current_user)
    rows = (
        db.query(Comment)
        .filter(Comment.video_id == video_id)
        .options(joinedload(Comment.user), joinedload(Comment.assignee))
        .order_by(Comment.timecode.asc(), Comment.created_at.asc())
        .all()
    )
    try:
        data, mime, filename = export_comments(rows, format, db_video.name or "Video")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return StreamingResponse(
        io.BytesIO(data),
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/", response_model=List[CommentWithRepliesResponse])
def get_comments(
    project_id: int,
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _, db_project = _check_video_access(project_id, video_id, db, current_user)

    rows = (
        db.query(Comment)
        .filter(Comment.video_id == video_id)
        .options(
            joinedload(Comment.likes),
            joinedload(Comment.user),
            joinedload(Comment.assignee),
        )
        .order_by(Comment.created_at.asc())
        .all()
    )
    return _build_comment_tree(rows, current_user, db, db_project)


@router.put("/{comment_id}", response_model=CommentResponse)
def update_comment(
    project_id: int,
    video_id: int,
    comment_id: int,
    comment: CommentUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _, db_project = _check_video_access(project_id, video_id, db, current_user)
    db_comment = (
        db.query(Comment)
        .filter(Comment.id == comment_id, Comment.video_id == video_id)
        .options(joinedload(Comment.likes), joinedload(Comment.user), joinedload(Comment.assignee))
        .first()
    )
    if not db_comment or not _comment_or_reply_visible(
        db, db_project, db_comment, current_user.id
    ):
        raise HTTPException(status_code=404, detail="Comment not found")

    team = _is_team_member(db, db_project, current_user.id)

    if comment.text is not None and db_comment.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to edit this comment")

    update_data = comment.dict(exclude_unset=True)
    if "text" in update_data:
        db_comment.text = update_data["text"]

    if "status" in update_data:
        if not team and db_comment.user_id != current_user.id:
            raise HTTPException(status_code=403, detail="Not authorized to change status")
        apply_status(db_comment, update_data["status"])
    elif "is_resolved" in update_data:
        if not team and db_comment.user_id != current_user.id:
            raise HTTPException(status_code=403, detail="Not authorized to resolve this comment")
        if update_data["is_resolved"]:
            apply_status(db_comment, COMMENT_STATUS_RESOLVED)
        else:
            apply_status(db_comment, COMMENT_STATUS_REOPENED)

    if "assignee_user_id" in update_data:
        if not team:
            raise HTTPException(status_code=403, detail="Not authorized to assign comments")
        db_comment.assignee_user_id = update_data["assignee_user_id"]

    if "due_at" in update_data:
        if not team and db_comment.user_id != current_user.id:
            raise HTTPException(status_code=403, detail="Not authorized to set due date")
        db_comment.due_at = update_data["due_at"]

    if "visibility" in update_data:
        if not team and db_comment.user_id != current_user.id:
            raise HTTPException(status_code=403, detail="Not authorized to change visibility")
        nv = normalize_visibility(update_data["visibility"], db_comment.is_private)
        db_comment.visibility = nv
        db_comment.is_private = nv != COMMENT_VISIBILITY_PUBLIC

    db.commit()
    db.refresh(db_comment)
    db_comment = (
        db.query(Comment)
        .filter(Comment.id == db_comment.id)
        .options(joinedload(Comment.likes), joinedload(Comment.user), joinedload(Comment.assignee))
        .first()
    )
    return _comment_response(db_comment, current_user.id)


@router.post("/bulk", response_model=dict)
def bulk_comment_action(
    project_id: int,
    video_id: int,
    body: CommentBulkAction,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _, db_project = _check_video_access(project_id, video_id, db, current_user)
    if not _is_team_member(db, db_project, current_user.id):
        raise HTTPException(status_code=403, detail="Not authorized")

    q = db.query(Comment).filter(Comment.video_id == video_id)
    if body.only_top_level:
        q = q.filter(Comment.parent_id.is_(None))
    if body.only_kind:
        q = q.filter(Comment.kind == body.only_kind)

    rows = q.order_by(Comment.id.asc()).limit(body.max_rows).all()
    updated = 0
    for c in rows:
        if body.action == "resolve":
            apply_status(c, COMMENT_STATUS_RESOLVED)
            updated += 1
        elif body.action == "set_status":
            if not body.set_status:
                raise HTTPException(status_code=400, detail="set_status required")
            apply_status(c, body.set_status)
            updated += 1
        elif body.action == "set_assignee":
            c.assignee_user_id = body.assignee_user_id
            updated += 1
    db.commit()
    return {"ok": True, "updated": updated}


@router.delete("/{comment_id}")
def delete_comment(
    project_id: int,
    video_id: int,
    comment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _, db_project = _check_video_access(project_id, video_id, db, current_user)
    db_comment = db.query(Comment).filter(Comment.id == comment_id, Comment.video_id == video_id).first()
    if not db_comment or not _comment_or_reply_visible(
        db, db_project, db_comment, current_user.id
    ):
        raise HTTPException(status_code=404, detail="Comment not found")
    team = _is_team_member(db, db_project, current_user.id)
    if db_comment.user_id != current_user.id and db_project.creator_id != current_user.id and not team:
        raise HTTPException(status_code=403, detail="Not authorized to delete this comment")

    db.delete(db_comment)
    db.commit()
    return {"message": "Comment deleted"}


@router.post("/{comment_id}/like", response_model=CommentResponse)
def toggle_like(
    project_id: int,
    video_id: int,
    comment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _, db_project = _check_video_access(project_id, video_id, db, current_user)
    db_comment = (
        db.query(Comment)
        .filter(Comment.id == comment_id, Comment.video_id == video_id)
        .options(joinedload(Comment.likes), joinedload(Comment.user), joinedload(Comment.assignee))
        .first()
    )
    if not db_comment or not _comment_or_reply_visible(
        db, db_project, db_comment, current_user.id
    ):
        raise HTTPException(status_code=404, detail="Comment not found")

    existing = db.query(CommentLike).filter(
        CommentLike.comment_id == comment_id,
        CommentLike.user_id == current_user.id,
    ).first()

    if existing:
        db.delete(existing)
    else:
        db.add(CommentLike(comment_id=comment_id, user_id=current_user.id))

    db.commit()
    db.refresh(db_comment)
    return _comment_response(db_comment, current_user.id)
