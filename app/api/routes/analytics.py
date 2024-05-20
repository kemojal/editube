from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.db.database import get_db
from app.api.models.analytics import ProjectAnalytics, UserAnalytics

router = APIRouter()

@router.get("/api/projects/{project_id}/analytics")
def get_project_analytics(project_id: int, db: Session = Depends(get_db)):
    analytics = db.query(ProjectAnalytics).filter(ProjectAnalytics.projectId == project_id).first()
    if not analytics:
        raise HTTPException(status_code=404, detail="Project analytics not found")
    return analytics

@router.get("/api/users/{user_id}/analytics")
def get_user_analytics(user_id: int, db: Session = Depends(get_db)):
    analytics = db.query(UserAnalytics).filter(UserAnalytics.userId == user_id).first()
    if not analytics:
        raise HTTPException(status_code=404, detail="User analytics not found")
    return analytics