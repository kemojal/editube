from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List

from app.db.database import get_db
from app.db.models import Project, Video, Comment, User
from app.api.models.comments import CommentCreate, CommentUpdate
from app.utils.security import get_current_user

router = APIRouter(
    prefix="/projects/{project_id}/videos/{video_id}/comments",
    tags=["Comments"],
)

@router.post("/")
def add_comment(project_id: int, video_id: int, comment: CommentCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    db_video = db.query(Video).filter(Video.id == video_id, Video.project_id == project_id).first()
    if not db_video:
        raise HTTPException(status_code=404, detail="Video not found")
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if current_user not in [db_project.creator] + [c.user for c in db_project.collaborators]:
        raise HTTPException(status_code=403, detail="Not authorized to add comments to this video")

    db_comment = Comment(
        video_id=video_id,
        user_id=current_user.id,
        text=comment.text,
        timecode=comment.timecode,
    )
    db.add(db_comment)
    db.commit()
    db.refresh(db_comment)
    return db_comment

@router.get("/")
def get_comments(project_id: int, video_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    db_video = db.query(Video).filter(Video.id == video_id, Video.project_id == project_id).first()
    if not db_video:
        raise HTTPException(status_code=404, detail="Video not found")
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if current_user not in [db_project.creator] + [c.user for c in db_project.collaborators]:
        raise HTTPException(status_code=403, detail="Not authorized to access comments for this video")
    return db_video.comments

@router.put("/{comment_id}")
def update_comment(project_id: int, video_id: int, comment_id: int, comment: CommentUpdate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    db_comment = db.query(Comment).filter(Comment.id == comment_id, Comment.video_id == video_id, Comment.video.project_id == project_id).first()
    if not db_comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if current_user not in [db_project.creator] + [c.user for c in db_project.collaborators]:
        raise HTTPException(status_code=403, detail="Not authorized to update comments for this video")
    if db_comment.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to update this comment")

    update_data = comment.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_comment, key, value)
    db.commit()
    db.refresh(db_comment)
    return db_comment

@router.delete("/{comment_id}")
def delete_comment(project_id: int, video_id: int, comment_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    db_comment = db.query(Comment).filter(Comment.id == comment_id, Comment.video_id == video_id, Comment.video.project_id == project_id).first()
    if not db_comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if current_user not in [db_project.creator] + [c.user for c in db_project.collaborators]:
        raise HTTPException(status_code=403, detail="Not authorized to delete comments for this video")
    if db_comment.user_id != current_user.id and db_project.creator_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this comment")

    db.delete(db_comment)
    db.commit()
    return db_comment