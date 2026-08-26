"""Project-scoped task-style comments (assignee + status + due)."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.db.database import get_db
from app.db.models import Comment, User, Video
from app.services.comment_workflow import COMMENT_KIND_CHANGE_REQUEST
from app.services.project_access import get_project_for_user
from app.utils.security import get_current_user

router = APIRouter(prefix="/projects/{project_id}/tasks", tags=["Tasks"])


@router.get("")
def list_project_tasks(
    project_id: int,
    mine: bool = Query(False, description="Only items assigned to the current user"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """The editor's punch list: change requests, plus anything assigned.

    This used to require an assignee, so a change request nobody had claimed —
    which is most of them, since clients don't assign work — never appeared in
    the tasks panel at all. An editor's checklist is the feedback they have to
    act on, whether or not someone put a name against it.
    """
    project = get_project_for_user(db, project_id, current_user)
    _ = project  # access validated
    q = (
        db.query(Comment)
        .join(Video, Video.id == Comment.video_id)
        .filter(
            Video.project_id == project_id,
            Comment.parent_id.is_(None),
            or_(
                Comment.assignee_user_id.isnot(None),
                Comment.kind == COMMENT_KIND_CHANGE_REQUEST,
            ),
        )
        .options(joinedload(Comment.assignee), joinedload(Comment.user), joinedload(Comment.video))
    )
    if mine:
        q = q.filter(Comment.assignee_user_id == current_user.id)
    # Timecode order: an editor works the timeline front to back, not in the
    # order comments happened to arrive.
    rows = (
        q.order_by(
            Comment.timecode.asc().nullslast(),
            Comment.created_at.asc(),
        )
        .limit(500)
        .all()
    )
    out: list[dict] = []
    for c in rows:
        out.append(
            {
                "id": c.id,
                "video_id": c.video_id,
                "video_name": c.video.name if c.video else None,
                "text": c.text,
                "timecode": c.timecode,
                "kind": getattr(c, "kind", None) or "comment",
                "status": getattr(c, "status", None) or "open",
                "due_at": c.due_at.isoformat() if c.due_at else None,
                "assignee_user_id": c.assignee_user_id,
                "assignee_name": (c.assignee.name or c.assignee.email) if c.assignee else None,
                "assignee_avatar_url": c.assignee.avatar_url if c.assignee else None,
                # Who raised it — a client's request reads differently from a
                # teammate's note.
                "author_name": (
                    (c.user.name or c.user.email) if c.user else (c.guest_name or None)
                ),
                "is_guest": c.user_id is None,
                # Set when the request was carried onto this version because it
                # was still open on the last one.
                "carried_from_comment_id": getattr(c, "carried_from_comment_id", None),
                "video_version": c.video.version if c.video else None,
                "video_thumbnail_url": c.video.thumbnail_url if c.video else None,
            }
        )
    return out
