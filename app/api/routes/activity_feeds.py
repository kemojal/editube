from __future__ import annotations

import json
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import ActivityFeed, Project, User, WorkspaceMember
from app.utils.security import get_current_user

router = APIRouter(prefix="/activity", tags=["Activity Feed"])


class ActivityItem(BaseModel):
    id: int
    action: str
    project_id: int
    project_name: str
    project_type: Optional[str] = None
    user_id: int
    user_name: str
    user_avatar_url: Optional[str] = None
    meta: dict
    created_at: str

    model_config = {"from_attributes": True}


@router.get("/feed", response_model=List[ActivityItem])
def get_global_activity_feed(
    limit: int = Query(20, le=50),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    workspace_ids = [
        m.workspace_id
        for m in db.query(WorkspaceMember).filter(WorkspaceMember.user_id == current_user.id).all()
    ]
    accessible_project_ids = [
        p.id
        for p in db.query(Project.id).filter(Project.workspace_id.in_(workspace_ids)).all()
    ]

    rows = (
        db.query(ActivityFeed)
        .filter(ActivityFeed.project_id.in_(accessible_project_ids))
        .order_by(ActivityFeed.created_at.desc())
        .limit(limit)
        .all()
    )

    project_map = {
        p.id: p
        for p in db.query(Project).filter(Project.id.in_({r.project_id for r in rows})).all()
    }
    user_map = {
        u.id: u
        for u in db.query(User).filter(User.id.in_({r.user_id for r in rows})).all()
    }

    result = []
    for row in rows:
        project = project_map.get(row.project_id)
        user = user_map.get(row.user_id)
        if not project or not user:
            continue
        try:
            meta = json.loads(row.meta_info or "{}")
        except (ValueError, TypeError):
            meta = {}
        result.append(ActivityItem(
            id=row.id,
            action=row.action,
            project_id=row.project_id,
            project_name=project.name,
            project_type=project.project_type,
            user_id=row.user_id,
            user_name=user.name,
            user_avatar_url=getattr(user, "avatar_url", None),
            meta=meta,
            created_at=row.created_at.isoformat(),
        ))
    return result
