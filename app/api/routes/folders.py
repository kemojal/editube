from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional, List

from app.db.database import get_db
from app.db.models import Project, Folder, Video, User
from app.services.project_access import assert_write_project_content, can_access_project
from app.api.models.folders import (
    FolderCreate,
    FolderUpdate,
    FolderResponse,
    VideoResponse,
    ProjectContentsResponse,
)
from app.utils.security import get_current_user

router = APIRouter(
    prefix="/projects/{project_id}/folders",
    tags=["Folders"],
)


def _check_project_access(db: Session, project_id: int, current_user: User) -> Project:
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_access_project(db, current_user.id, db_project):
        raise HTTPException(status_code=403, detail="Not authorized to access this project")
    return db_project


def _build_breadcrumb(db: Session, folder_id: Optional[int]) -> List[FolderResponse]:
    breadcrumb = []
    current_id = folder_id
    while current_id is not None:
        folder = db.query(Folder).filter(Folder.id == current_id).first()
        if not folder:
            break
        breadcrumb.insert(
            0,
            FolderResponse(
                id=folder.id,
                name=folder.name,
                project_id=folder.project_id,
                parent_id=folder.parent_id,
                created_by=folder.created_by,
                created_at=folder.created_at,
                updated_at=folder.updated_at,
            ),
        )
        current_id = folder.parent_id
    return breadcrumb


@router.get("/contents", response_model=ProjectContentsResponse)
def get_project_contents(
    project_id: int,
    parent_id: Optional[int] = Query(None, description="Parent folder ID, null for root"),
    search: Optional[str] = Query(None, description="Search query for folder/video names"),
    sort_by: str = Query("name", description="Sort by: name, created_at, updated_at"),
    sort_order: str = Query("asc", description="Sort order: asc, desc"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_project_access(db, project_id, current_user)

    folders_query = db.query(Folder).filter(Folder.project_id == project_id)
    videos_query = db.query(Video).filter(Video.project_id == project_id)

    if search:
        search_term = f"%{search}%"
        folders_query = folders_query.filter(Folder.name.ilike(search_term))
        videos_query = videos_query.filter(Video.name.ilike(search_term))
    else:
        folders_query = folders_query.filter(Folder.parent_id == parent_id)
        videos_query = videos_query.filter(Video.folder_id == parent_id)

    # Sorting
    folder_sort_col = getattr(Folder, sort_by, Folder.name)
    video_sort_col = getattr(Video, sort_by, Video.name)
    if sort_order == "desc":
        folders_query = folders_query.order_by(folder_sort_col.desc())
        videos_query = videos_query.order_by(video_sort_col.desc())
    else:
        folders_query = folders_query.order_by(folder_sort_col.asc())
        videos_query = videos_query.order_by(video_sort_col.asc())

    folders = folders_query.all()
    videos = videos_query.all()

    breadcrumb = _build_breadcrumb(db, parent_id)

    folder_responses = [
        FolderResponse(
            id=f.id,
            name=f.name,
            project_id=f.project_id,
            parent_id=f.parent_id,
            created_by=f.created_by,
            created_at=f.created_at,
            updated_at=f.updated_at,
        )
        for f in folders
    ]
    video_responses = [
        VideoResponse(
            id=v.id,
            name=v.name,
            description=v.description,
            version=v.version,
            file_path=v.file_path,
            thumbnail_url=v.thumbnail_url,
            project_id=v.project_id,
            folder_id=v.folder_id,
            uploader_id=v.uploader_id,
            created_at=v.created_at,
            updated_at=v.updated_at,
        )
        for v in videos
    ]

    return ProjectContentsResponse(
        folders=folder_responses,
        videos=video_responses,
        breadcrumb=breadcrumb,
        total_items=len(folders) + len(videos),
    )


@router.post("/", response_model=FolderResponse)
def create_folder(
    project_id: int,
    folder_data: FolderCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db_project = _check_project_access(db, project_id, current_user)
    assert_write_project_content(db, current_user, db_project)

    if folder_data.parent_id is not None:
        parent = db.query(Folder).filter(
            Folder.id == folder_data.parent_id,
            Folder.project_id == project_id,
        ).first()
        if not parent:
            raise HTTPException(status_code=404, detail="Parent folder not found")

    db_folder = Folder(
        name=folder_data.name,
        project_id=project_id,
        parent_id=folder_data.parent_id,
        created_by=current_user.id,
    )
    db.add(db_folder)
    db.commit()
    db.refresh(db_folder)
    return db_folder


@router.put("/{folder_id}", response_model=FolderResponse)
def update_folder(
    project_id: int,
    folder_id: int,
    folder_data: FolderUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db_project = _check_project_access(db, project_id, current_user)
    assert_write_project_content(db, current_user, db_project)

    db_folder = db.query(Folder).filter(
        Folder.id == folder_id, Folder.project_id == project_id
    ).first()
    if not db_folder:
        raise HTTPException(status_code=404, detail="Folder not found")

    if folder_data.name is not None:
        db_folder.name = folder_data.name
    if folder_data.parent_id is not None:
        # Prevent circular references
        if folder_data.parent_id == folder_id:
            raise HTTPException(status_code=400, detail="Folder cannot be its own parent")
        db_folder.parent_id = folder_data.parent_id

    db.commit()
    db.refresh(db_folder)
    return db_folder


@router.delete("/{folder_id}")
def delete_folder(
    project_id: int,
    folder_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db_project = _check_project_access(db, project_id, current_user)
    assert_write_project_content(db, current_user, db_project)

    db_folder = db.query(Folder).filter(
        Folder.id == folder_id, Folder.project_id == project_id
    ).first()
    if not db_folder:
        raise HTTPException(status_code=404, detail="Folder not found")

    db.delete(db_folder)
    db.commit()
    return {"message": "Folder deleted successfully"}


@router.get("/{folder_id}", response_model=FolderResponse)
def get_folder(
    project_id: int,
    folder_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_project_access(db, project_id, current_user)

    db_folder = db.query(Folder).filter(
        Folder.id == folder_id, Folder.project_id == project_id
    ).first()
    if not db_folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    return db_folder
