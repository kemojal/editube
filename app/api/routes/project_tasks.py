"""Project-scoped task-style comments (assignee + status + due)."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session, joinedload

from app.db.database import get_db
from app.db.models import Comment, User, Video
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
    project = get_project_for_user(db, project_id, current_user)
    _ = project  # access validated
    q = (
        db.query(Comment)
        .join(Video, Video.id == Comment.video_id)
        .filter(
            Video.project_id == project_id,
            Comment.parent_id.is_(None),
            Comment.assignee_user_id.isnot(None),
        )
        .options(joinedload(Comment.assignee), joinedload(Comment.user), joinedload(Comment.video))
    )
    if mine:
        q = q.filter(Comment.assignee_user_id == current_user.id)
    rows = q.order_by(Comment.due_at.asc().nullslast(), Comment.created_at.desc()).limit(500).all()
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
            }
        )
    return out
