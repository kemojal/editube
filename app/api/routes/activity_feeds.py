from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List

from app.db.database import get_db
from app.db.models import Project, ActivityFeed, User
from app.services.project_access import can_access_project
from app.utils.security import get_current_user

router = APIRouter(
    prefix="/projects/{project_id}/activity",
    tags=["Activity Feed"],
)

@router.get("/")
def get_activity_feed(project_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_access_project(db, current_user.id, db_project):
        raise HTTPException(status_code=403, detail="Not authorized to access this project's activity feed")
    activity_feed = db.query(ActivityFeed).filter(ActivityFeed.project_id == project_id).all()
    return activity_feed