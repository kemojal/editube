# from fastapi import APIRouter, Depends, HTTPException
# from sqlalchemy.orm import Session

# from app.db.database import get_db
# from app.db.models import Project, ProjectCollaborator, User
# from app.api.models.projects import ProjectCreate, ProjectUpdate

# router = APIRouter(
#     prefix="/projects",
#     tags=["Projects"],
# )

# @router.post("/", response_model=Project)
# def create_project(project: ProjectCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
#     db_project = Project(name=project.name, description=project.description, creator_id=current_user.id)
#     db.add(db_project)
#     db.commit()
#     db


from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
import logging

from app.db.database import get_db
from app.db.models import Project, ProjectCollaborator, User
from app.api.models.projects import ProjectCreate, ProjectUpdate, CollaboratorEmailList, ProjectResponse, UserResponse
from app.utils.security import get_current_user
from app.utils.email import send_invitation_email


logger = logging.getLogger(__name__)  # Define or import logger

router = APIRouter(
    prefix="/projects",
    tags=["Projects"],
)


def convert_project_to_response(db_project: Project) -> ProjectResponse:
    return ProjectResponse(
        id=db_project.id,
        name=db_project.name,
        description=db_project.description,
        created_at=db_project.created_at.isoformat(),
        updated_at=db_project.updated_at.isoformat(),
        creator=UserResponse(
            id=db_project.creator.id,
            name=db_project.creator.name,
            email=db_project.creator.email,
            created_at=db_project.creator.created_at.isoformat(),
            updated_at=db_project.creator.updated_at.isoformat()
        ),
        collaborators=[
            UserResponse(
                id=collaborator.user.id,
                name=collaborator.user.name,
                email=collaborator.user.email,
                created_at=collaborator.user.created_at.isoformat(),
                updated_at=collaborator.user.updated_at.isoformat()
            ) for collaborator in db_project.collaborators
        ]
    )

@router.post("/")
def create_project(project: ProjectCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    db_project = Project(name=project.name, description=project.description, creator_id=current_user.id)
    db.add(db_project)
    db.commit()
    db.refresh(db_project)
    return db_project

@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(project_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    # db_project = db.query(Project).filter(Project.id == project_id).first()
    # if not db_project:
    #     raise HTTPException(status_code=404, detail="Project not found")
    # if current_user not in [db_project.creator] + [c.user for c in db_project.collaborators]:
    #     raise HTTPException(status_code=403, detail="Not authorized to access this project")
    # return db_project
    try:
        db_project = db.query(Project).filter(Project.id == project_id).first()
        if not db_project:
            raise HTTPException(status_code=404, detail="Project not found")
        if current_user not in [db_project.creator] + [c.user for c in db_project.collaborators]:
            raise HTTPException(status_code=403, detail="Not authorized to access this project")
        return convert_project_to_response(db_project)
    except Exception as e:
        logger.error(f"Error retrieving project {project_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@router.put("/{project_id}", response_model=ProjectResponse)
def update_project(project_id: int, project: ProjectUpdate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")
    if db_project.creator_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to update this project")
    update_data = project.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_project, key, value)
    db.commit()
    db.refresh(db_project)
    return convert_project_to_response(db_project)
    # return db_project

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
    current_user: User = Depends(get_current_user)
):
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")
    if db_project.creator_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to invite collaborators to this project")
    
    new_collaborators = []
    for email in email_list.collaborator_emails:
        collaborator = db.query(User).filter(User.email == email).first()
        if collaborator:
            if collaborator in [c.user for c in db_project.collaborators]:
                raise HTTPException(status_code=400, detail=f"User with email {email} is already a collaborator")
            user_id = collaborator.id
        else:
            # Send an invitation email if the user doesn't exist
            send_invitation_email(email, project_id)  # Implement this function in utils/email.py if not already done

            # You may want to store invited users in your database for tracking purposes
            # For example:
            # invited_user = InvitedUser(email=email, project_id=project_id)
            # db.add(invited_user)
            # db.commit()
            # db.refresh(invited_user)

            user_id = 8 # Set user_id to None or another appropriate value

        new_collaborator = ProjectCollaborator(project_id=project_id, user_id=user_id, role="collaborator")
        db.add(new_collaborator)
        new_collaborators.append(new_collaborator)
    db.commit()
    return new_collaborators




@router.delete("/{project_id}/collaborators/{user_id}")
def remove_collaborator(project_id: int, user_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")
    if db_project.creator_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to remove collaborators from this project")
    collaborator = db.query(ProjectCollaborator).filter(ProjectCollaborator.project_id == project_id, ProjectCollaborator.user_id == user_id).first()
    if not collaborator:
        raise HTTPException(status_code=404, detail="Collaborator not found for this project")
    db.delete(collaborator)
    db.commit()
    db.refresh(db_project)
    return convert_project_to_response(db_project)
    # return db_project


@router.get("/")
def get_user_projects(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    created_projects = db.query(Project).filter(Project.creator_id == current_user.id).all()
    collaborated_projects = db.query(Project).join(ProjectCollaborator).filter(ProjectCollaborator.user_id == current_user.id).all()
    all_projects = created_projects + collaborated_projects
    return [convert_project_to_response(project) for project in all_projects]
    # return all_projects