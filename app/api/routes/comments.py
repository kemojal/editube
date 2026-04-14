from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List

from app.db.database import get_db
from app.db.models import Project, Video, Comment, CommentLike, User
from app.api.models.comments import (
    CommentCreate, CommentUpdate, CommentResponse, CommentWithRepliesResponse,
)
from app.utils.security import get_current_user

router = APIRouter(
    prefix="/projects/{project_id}/videos/{video_id}/comments",
    tags=["Comments"],
)


def _check_video_access(project_id: int, video_id: int, db: Session, current_user: User):
    db_video = db.query(Video).filter(Video.id == video_id, Video.project_id == project_id).first()
    if not db_video:
        raise HTTPException(status_code=404, detail="Video not found")
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if current_user not in [db_project.creator] + [c.user for c in db_project.collaborators]:
        raise HTTPException(status_code=403, detail="Not authorized")
    return db_video, db_project


def _comment_response(comment: Comment, current_user_id: int) -> dict:
    likes_count = len(comment.likes) if comment.likes else 0
    liked_by_me = any(like.user_id == current_user_id for like in (comment.likes or []))
    replies_count = len(comment.replies) if comment.replies else 0
    return {
        "id": comment.id,
        "video_id": comment.video_id,
        "parent_id": comment.parent_id,
        "text": comment.text,
        "timecode": comment.timecode,
        "end_timecode": comment.end_timecode,
        "drawing_data": comment.drawing_data,
        "is_resolved": comment.is_resolved,
        "user": comment.user,
        "likes_count": likes_count,
        "liked_by_me": liked_by_me,
        "replies_count": replies_count,
        "created_at": comment.created_at,
        "updated_at": comment.updated_at,
    }


@router.post("/", response_model=CommentResponse)
def add_comment(
    project_id: int,
    video_id: int,
    comment: CommentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_access(project_id, video_id, db, current_user)

    if comment.parent_id is not None:
        parent = db.query(Comment).filter(
            Comment.id == comment.parent_id, Comment.video_id == video_id
        ).first()
        if not parent:
            raise HTTPException(status_code=404, detail="Parent comment not found")

    db_comment = Comment(
        video_id=video_id,
        user_id=current_user.id,
        parent_id=comment.parent_id,
        text=comment.text,
        timecode=comment.timecode,
        end_timecode=comment.end_timecode,
        drawing_data=comment.drawing_data,
    )
    db.add(db_comment)
    db.commit()
    db.refresh(db_comment)
    return _comment_response(db_comment, current_user.id)


@router.get("/", response_model=List[CommentWithRepliesResponse])
def get_comments(
    project_id: int,
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_access(project_id, video_id, db, current_user)

    # Get top-level comments only (no parent)
    top_comments = (
        db.query(Comment)
        .filter(Comment.video_id == video_id, Comment.parent_id.is_(None))
        .order_by(Comment.timecode.asc(), Comment.created_at.asc())
        .all()
    )

    result = []
    for comment in top_comments:
        data = _comment_response(comment, current_user.id)
        data["replies"] = [
            _comment_response(reply, current_user.id)
            for reply in sorted(comment.replies or [], key=lambda r: r.created_at)
        ]
        result.append(data)
    return result


@router.put("/{comment_id}", response_model=CommentResponse)
def update_comment(
    project_id: int,
    video_id: int,
    comment_id: int,
    comment: CommentUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_access(project_id, video_id, db, current_user)
    db_comment = db.query(Comment).filter(Comment.id == comment_id, Comment.video_id == video_id).first()
    if not db_comment:
        raise HTTPException(status_code=404, detail="Comment not found")

    # Only the author can edit text; project creator/collaborators can resolve
    if comment.text is not None and db_comment.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to edit this comment")

    update_data = comment.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_comment, key, value)
    db.commit()
    db.refresh(db_comment)
    return _comment_response(db_comment, current_user.id)


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
    if not db_comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    if db_comment.user_id != current_user.id and db_project.creator_id != current_user.id:
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
    _check_video_access(project_id, video_id, db, current_user)
    db_comment = db.query(Comment).filter(Comment.id == comment_id, Comment.video_id == video_id).first()
    if not db_comment:
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
