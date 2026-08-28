"""Project authorization: creator, collaborators, and workspace members (agency)."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.db.models import Project, ProjectCollaborator, User, WorkspaceMember
from app.services.request_context import bind_workspace


def get_workspace_member(
    db: Session, workspace_id: int | None, user_id: int
) -> WorkspaceMember | None:
    if not workspace_id:
        return None
    return (
        db.query(WorkspaceMember)
        .filter(WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user_id)
        .first()
    )


def can_access_project(db: Session, user_id: int, project: Project) -> bool:
    if project.creator_id == user_id:
        bind_workspace(project.workspace_id)
        return True
    if (
        db.query(ProjectCollaborator)
        .filter(ProjectCollaborator.project_id == project.id, ProjectCollaborator.user_id == user_id)
        .first()
    ):
        bind_workspace(project.workspace_id)
        return True
    wm = get_workspace_member(db, project.workspace_id, user_id)
    if not wm:
        return False
    if wm.role == "client":
        return False
    bind_workspace(project.workspace_id)
    return True


def assert_project_access(db: Session, user: User, project: Project) -> None:
    if not can_access_project(db, user.id, project):
        raise HTTPException(status_code=403, detail="Not authorized")


def get_project_for_user(db: Session, project_id: int, user: User) -> Project:
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    assert_project_access(db, user, project)
    return project


def can_moderate_video_comments(db: Session, project: Project, user_id: int) -> bool:
    """
    Users who may change comment workflow fields on others' comments (status, assignee, etc.).
    Matches comments API "team": creator, non-client collaborators, non-client workspace members.
    """
    if project.creator_id == user_id:
        return True
    row = (
        db.query(ProjectCollaborator)
        .filter(ProjectCollaborator.project_id == project.id, ProjectCollaborator.user_id == user_id)
        .first()
    )
    if row:
        return str(row.role or "").lower() != "client"
    wm = get_workspace_member(db, project.workspace_id, user_id)
    return bool(wm and wm.role not in ("client", "guest"))


def collaborator_role(db: Session, project_id: int, user_id: int) -> str | None:
    row = (
        db.query(ProjectCollaborator)
        .filter(ProjectCollaborator.project_id == project_id, ProjectCollaborator.user_id == user_id)
        .first()
    )
    return row.role if row else None


def can_write_project_content(db: Session, user_id: int, project: Project) -> bool:
    if project.creator_id == user_id:
        return True
    cr = collaborator_role(db, project.id, user_id)
    if cr and str(cr).lower() == "client":
        return False
    if cr:
        return True
    wm = get_workspace_member(db, project.workspace_id, user_id)
    if wm and wm.role in ("owner", "producer", "editor", "assistant"):
        return True
    return False


def assert_write_project_content(db: Session, user: User, project: Project) -> None:
    if not can_write_project_content(db, user.id, project):
        raise HTTPException(status_code=403, detail="Not authorized to modify this project")


def can_manage_project_settings(db: Session, user_id: int, project: Project) -> bool:
    if project.creator_id == user_id:
        return True
    wm = get_workspace_member(db, project.workspace_id, user_id)
    if wm and wm.role in ("owner", "producer"):
        return True
    cr = collaborator_role(db, project.id, user_id)
    if cr and str(cr).lower() in ("producer", "owner"):
        return True
    return False


def can_manage_freelancer_financials(db: Session, user_id: int, project: Project) -> bool:
    if project.creator_id == user_id:
        return True
    wm = get_workspace_member(db, project.workspace_id, user_id)
    return bool(wm and wm.role in ("owner", "producer"))


def assert_manage_freelancer_financials(db: Session, user: User, project: Project) -> None:
    if not can_manage_freelancer_financials(db, user.id, project):
        raise HTTPException(status_code=403, detail="Not authorized")


def list_users_for_mentions(db: Session, project: Project) -> list[User]:
    seen: dict[int, User] = {}
    if project.creator:
        seen[project.creator.id] = project.creator
    for c in project.collaborators or []:
        if c.user:
            seen[c.user.id] = c.user
    wms = (
        db.query(WorkspaceMember)
        .filter(WorkspaceMember.workspace_id == project.workspace_id)
        .all()
    )
    for wm in wms:
        if wm.role == "client":
            continue
        u = wm.user
        if u:
            seen[u.id] = u
    return list(seen.values())
