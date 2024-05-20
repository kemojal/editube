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

from app.db.database import get_db
from app.db.models import Project, ProjectCollaborator, User
from app.api.models.projects import ProjectCreate, ProjectUpdate
from app.utils.security import get_current_user

router = APIRouter(
    prefix="/projects",
    tags=["Projects"],
)

@router.post("/")
def create_project(project: ProjectCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    db_project = Project(name=project.name, description=project.description, creator_id=current_user.id)
    db.add(db_project)
    db.commit()
    db.refresh(db_project)
    return db_project

# @router.get("/{project_id}", response_model=Project)
# def get_project(project_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
#     db_project = db.query(Project).filter(Project.id == project_id).first()
#     if not db_project:
#         raise HTTPException(status_code=404, detail="Project not found")
#     if current_user not in [db_project.creator] + [c.user for c in db_project.collaborators]:
#         raise HTTPException(status_code=403, detail="Not authorized to access this project")
#     return db_project

# @router.put("/{project_id}", response_model=Project)
# def update_project(project_id: int, project: ProjectUpdate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
#     db_project = db.query(Project).filter(Project.id == project_id).first()
#     if not db_project:
#         raise HTTPException(status_code=404, detail="Project not found")
#     if db_project.creator_id != current_user.id:
#         raise HTTPException(status_code=403, detail="Not authorized to update this project")
#     update_data = project.dict(exclude_unset=True)
#     for key, value in update_data.items():
#         setattr(db_project, key, value)
#     db.commit()
#     db.refresh(db_project)
#     return db_project

# @router.delete("/{project_id}")
# def delete_project(project_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
#     db_project = db.query(Project).filter(Project.id == project_id).first()
#     if not db_project:
#         raise HTTPException(status_code=404, detail="Project not found")
#     if db_project.creator_id != current_user.id:
#         raise HTTPException(status_code=403, detail="Not authorized to delete this project")
#     db.delete(db_project)
#     db.commit()
#     return {"message": "Project deleted successfully"}

# @router.post("/{project_id}/collaborators", response_model=List[ProjectCollaborator])
# def invite_collaborators(project_id: int, collaborator_emails: List[str], db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
#     db_project = db.query(Project).filter(Project.id == project_id).first()
#     if not db_project:
#         raise HTTPException(status_code=404, detail="Project not found")
#     if db_project.creator_id != current_user.id:
#         raise HTTPException(status_code=403, detail="Not authorized to invite collaborators to this project")
#     new_collaborators = []
#     for email in collaborator_emails:
#         collaborator = db.query(User).filter(User.email == email).first()
#         if not collaborator:
#             raise HTTPException(status_code=404, detail=f"User with email {email} not found")
#         if collaborator in [c.user for c in db_project.collaborators]:
#             raise HTTPException(status_code=400, detail=f"User with email {email} is already a collaborator")
#         new_collaborator = ProjectCollaborator(project_id=project_id, user_id=collaborator.id, role="collaborator")
#         db.add(new_collaborator)
#         new_collaborators.append(new_collaborator)
#     db.commit()
#     return new_collaborators

# @router.delete("/{project_id}/collaborators/{user_id}", response_model=Project)
# def remove_collaborator(project_id: int, user_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
#     db_project = db.query(Project).filter(Project.id == project_id).first()
#     if not db_project:
#         raise HTTPException(status_code=404, detail="Project not found")
#     if db_project.creator_id != current_user.id:
#         raise HTTPException(status_code=403, detail="Not authorized to remove collaborators from this project")
#     collaborator = db.query(ProjectCollaborator).filter(ProjectCollaborator.project_id == project_id, ProjectCollaborator.user_id == user_id).first()
#     if not collaborator:
#         raise HTTPException(status_code=404, detail="Collaborator not found for this project")
#     db.delete(collaborator)
#     db.commit()
#     db.refresh(db_project)
#     return db_project