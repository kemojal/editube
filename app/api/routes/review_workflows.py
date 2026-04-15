"""Project-scoped review approval workflow templates (ordered stages)."""

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import Project, ReviewWorkflowStage, ReviewWorkflowTemplate, User
from app.api.models.review_workflow import (
    ReviewWorkflowTemplateCreate,
    ReviewWorkflowTemplateResponse,
    ReviewWorkflowStageResponse,
)
from app.utils.security import get_current_user
from app.services.project_access import can_access_project

router = APIRouter(
    prefix="/projects/{project_id}/review-workflow-templates",
    tags=["Review workflows"],
)


def _project_access(project_id: int, db: Session, current_user: User) -> Project:
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_access_project(db, current_user.id, project):
        raise HTTPException(status_code=403, detail="Not authorized")
    return project


def _template_to_response(t: ReviewWorkflowTemplate, db: Session) -> dict:
    stages = (
        db.query(ReviewWorkflowStage)
        .filter(ReviewWorkflowStage.template_id == t.id)
        .order_by(ReviewWorkflowStage.stage_index.asc())
        .all()
    )
    return {
        "id": t.id,
        "project_id": t.project_id,
        "name": t.name,
        "created_at": t.created_at,
        "updated_at": t.updated_at,
        "stages": [
            ReviewWorkflowStageResponse(
                id=s.id,
                template_id=s.template_id,
                stage_index=s.stage_index,
                stage_key=s.stage_key,
                label=s.label,
                notify_user_ids=list(s.notify_user_ids or []),
            )
            for s in stages
        ],
    }


@router.get("", response_model=List[ReviewWorkflowTemplateResponse])
def list_templates(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _project_access(project_id, db, current_user)
    rows = (
        db.query(ReviewWorkflowTemplate)
        .filter(ReviewWorkflowTemplate.project_id == project_id)
        .order_by(ReviewWorkflowTemplate.created_at.desc())
        .all()
    )
    return [_template_to_response(t, db) for t in rows]


@router.post("", response_model=ReviewWorkflowTemplateResponse)
def create_template(
    project_id: int,
    body: ReviewWorkflowTemplateCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _project_access(project_id, db, current_user)
    t = ReviewWorkflowTemplate(project_id=project_id, name=body.name.strip() or "Workflow")
    db.add(t)
    db.flush()
    for idx, st in enumerate(body.stages):
        db.add(
            ReviewWorkflowStage(
                template_id=t.id,
                stage_index=idx,
                stage_key=st.stage_key.strip() or f"stage_{idx}",
                label=st.label.strip() or st.stage_key,
                notify_user_ids=list(st.notify_user_ids or []),
            )
        )
    db.commit()
    db.refresh(t)
    return _template_to_response(t, db)


@router.delete("/{template_id}")
def delete_template(
    project_id: int,
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _project_access(project_id, db, current_user)
    t = (
        db.query(ReviewWorkflowTemplate)
        .filter(
            ReviewWorkflowTemplate.id == template_id,
            ReviewWorkflowTemplate.project_id == project_id,
        )
        .first()
    )
    if not t:
        raise HTTPException(status_code=404, detail="Template not found")
    db.delete(t)
    db.commit()
    return {"ok": True}
