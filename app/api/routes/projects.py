from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.models.projects import (
    CollaboratorEmailList,
    ProjectCollaboratorUpdate,
    ProjectCreate,
    ProjectResponse,
    ProjectUpdate,
    UserResponse,
)
from app.db.database import get_db
from app.db.models import Project, ProjectCollaborator, ProjectTemplate, User, WorkspaceMember
from app.services.project_access import (
    can_access_project,
    can_manage_project_settings,
    get_workspace_member,
)
from app.services.project_template_apply import apply_project_template
from app.services.workspace_bootstrap import ensure_personal_workspace
from app.utils.email import send_invitation_email
from app.utils.security import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/projects",
    tags=["Projects"],
)


def convert_project_to_response(db_project: Project) -> ProjectResponse:
    return ProjectResponse(
        id=db_project.id,
        name=db_project.name,
        description=db_project.description,
        workspace_id=db_project.workspace_id,
        created_at=db_project.created_at.isoformat(),
        updated_at=db_project.updated_at.isoformat(),
        creator=UserResponse(
            id=db_project.creator.id,
            name=db_project.creator.name,
            email=db_project.creator.email,
            created_at=db_project.creator.created_at.isoformat(),
            updated_at=db_project.creator.updated_at.isoformat(),
        ),
        collaborators=[
            UserResponse(
                id=collaborator.user.id,
                name=collaborator.user.name,
                email=collaborator.user.email,
                created_at=collaborator.user.created_at.isoformat(),
                updated_at=collaborator.user.updated_at.isoformat(),
            )
            for collaborator in db_project.collaborators
        ],
    )


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
    else:
        ws = ensure_personal_workspace(db, current_user)
        ws_id = ws.id

    db_project = Project(
        name=project.name,
        description=project.description,
        creator_id=current_user.id,
        workspace_id=ws_id,
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

    db.commit()
    db.refresh(db_project)
    return convert_project_to_response(db_project)


@router.get("/", response_model=List[ProjectResponse])
def get_user_projects(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    created_projects = db.query(Project).filter(Project.creator_id == current_user.id).all()
    collaborated_projects = (
        db.query(Project).join(ProjectCollaborator).filter(ProjectCollaborator.user_id == current_user.id).all()
    )
    ws_ids = [
        r.workspace_id
        for r in db.query(WorkspaceMember)
        .filter(WorkspaceMember.user_id == current_user.id, WorkspaceMember.role != "client")
        .all()
    ]
    ws_projects = (
        db.query(Project).filter(Project.workspace_id.in_(ws_ids)).all()
        if ws_ids
        else []
    )
    merged: dict[int, Project] = {}
    for p in created_projects + collaborated_projects + ws_projects:
        merged[p.id] = p
    return [convert_project_to_response(project) for project in merged.values()]


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(project_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    try:
        db_project = db.query(Project).filter(Project.id == project_id).first()
        if not db_project:
            raise HTTPException(status_code=404, detail="Project not found")
        if not can_access_project(db, current_user.id, db_project):
            raise HTTPException(status_code=403, detail="Not authorized to access this project")
        return convert_project_to_response(db_project)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error retrieving project %s", project_id)
        raise HTTPException(status_code=500, detail="Internal server error") from e


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
    return convert_project_to_response(db_project)


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


@router.post("/{project_id}/collaborators")
def invite_collaborators(
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
        collaborator = db.query(User).filter(User.email == em).first()
        if collaborator:
            if collaborator in [c.user for c in db_project.collaborators]:
                raise HTTPException(status_code=400, detail=f"User with email {em} is already a collaborator")
            new_collaborator = ProjectCollaborator(
                project_id=project_id,
                user_id=collaborator.id,
                role=role,
            )
            db.add(new_collaborator)
            _ensure_workspace_collaborator(db, db_project, collaborator.id)
            new_collaborators.append(new_collaborator)
        else:
            send_invitation_email(db, em, project_id)

    db.commit()
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
    return convert_project_to_response(db_project)


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
    return convert_project_to_response(db_project)
