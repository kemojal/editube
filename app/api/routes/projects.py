from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload

from app.api.models.projects import (
    CollaboratorEmailList,
    ProjectCollaboratorUpdate,
    ProjectCreate,
    ProjectResponse,
    ProjectUpdate,
    UserResponse,
    WorkspaceAssetLinkCreate,
    WorkspaceAssetLinkResponse,
)
from app.db.database import get_db
from app.db.models import (
    Folder,
    Project,
    ProjectCollaborator,
    ProjectTemplate,
    ProjectWorkspaceAssetLink,
    User,
    WorkspaceAsset,
    WorkspaceMember,
)
from app.services.project_access import (
    assert_write_project_content,
    can_access_project,
    can_manage_project_settings,
    get_project_for_user,
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
        if wm.role in ("client", "guest"):
            raise HTTPException(
                status_code=403,
                detail="Not allowed to create projects in this workspace with your role",
            )
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


def _workspace_asset_link_dict(link: ProjectWorkspaceAssetLink) -> dict:
    a = link.workspace_asset
    return {
        "id": link.id,
        "project_id": link.project_id,
        "workspace_asset_id": link.workspace_asset_id,
        "folder_id": link.folder_id,
        "category": a.category if a else "",
        "title": a.title if a else "",
        "file_url": a.file_url if a else "",
        "created_at": link.created_at,
    }


@router.get("/{project_id}/workspace-assets", response_model=List[WorkspaceAssetLinkResponse])
def list_project_workspace_assets(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = get_project_for_user(db, project_id, current_user)
    rows = (
        db.query(ProjectWorkspaceAssetLink)
        .options(joinedload(ProjectWorkspaceAssetLink.workspace_asset))
        .filter(ProjectWorkspaceAssetLink.project_id == project.id)
        .order_by(ProjectWorkspaceAssetLink.created_at.desc())
        .all()
    )
    return [_workspace_asset_link_dict(r) for r in rows]


@router.post("/{project_id}/workspace-assets", response_model=WorkspaceAssetLinkResponse)
def attach_workspace_asset_to_project(
    project_id: int,
    body: WorkspaceAssetLinkCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db_project = get_project_for_user(db, project_id, current_user)
    assert_write_project_content(db, current_user, db_project)

    asset = (
        db.query(WorkspaceAsset)
        .filter(WorkspaceAsset.id == body.workspace_asset_id)
        .first()
    )
    if not asset or asset.workspace_id != db_project.workspace_id:
        raise HTTPException(status_code=404, detail="Workspace asset not found in this workspace")

    if body.folder_id is not None:
        folder = (
            db.query(Folder)
            .filter(Folder.id == body.folder_id, Folder.project_id == project_id)
            .first()
        )
        if not folder:
            raise HTTPException(status_code=404, detail="Folder not found in this project")
    else:
        folder = None

    existing = (
        db.query(ProjectWorkspaceAssetLink)
        .filter(
            ProjectWorkspaceAssetLink.project_id == project_id,
            ProjectWorkspaceAssetLink.workspace_asset_id == body.workspace_asset_id,
        )
        .first()
    )
    if existing:
        existing.folder_id = body.folder_id
        db.commit()
        existing = (
            db.query(ProjectWorkspaceAssetLink)
            .filter(ProjectWorkspaceAssetLink.id == existing.id)
            .options(joinedload(ProjectWorkspaceAssetLink.workspace_asset))
            .first()
        )
        return _workspace_asset_link_dict(existing)

    link = ProjectWorkspaceAssetLink(
        project_id=project_id,
        workspace_asset_id=body.workspace_asset_id,
        folder_id=body.folder_id,
        created_by_user_id=current_user.id,
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    link = (
        db.query(ProjectWorkspaceAssetLink)
        .filter(ProjectWorkspaceAssetLink.id == link.id)
        .options(joinedload(ProjectWorkspaceAssetLink.workspace_asset))
        .first()
    )
    return _workspace_asset_link_dict(link)


@router.delete("/{project_id}/workspace-assets/{link_id}")
def detach_workspace_asset_from_project(
    project_id: int,
    link_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db_project = get_project_for_user(db, project_id, current_user)
    assert_write_project_content(db, current_user, db_project)
    link = (
        db.query(ProjectWorkspaceAssetLink)
        .filter(
            ProjectWorkspaceAssetLink.id == link_id,
            ProjectWorkspaceAssetLink.project_id == project_id,
        )
        .first()
    )
    if not link:
        raise HTTPException(status_code=404, detail="Link not found")
    db.delete(link)
    db.commit()
    return {"ok": True}
