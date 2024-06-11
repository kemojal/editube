from fastapi import APIRouter, Depends, HTTPException, File, UploadFile
from sqlalchemy.orm import Session
from typing import List

from app.db.database import get_db
from app.db.models import Project, Video, User
from app.api.models.videos import VideoCreate, VideoUpdate
from app.utils.security import get_current_user
from app.utils.storage import upload_file, delete_file
# import app.utils.cloudinary as upload_file_to_cloudinary
from app.utils.cloudinary import upload_file_to_cloudinary

router = APIRouter(
    prefix="/projects/{project_id}/videos",
    tags=["Videos"],
)

# @router.post("/")
# def upload_video(project_id: int, video: VideoCreate, video_file: UploadFile = File(...), db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
#     db_project = db.query(Project).filter(Project.id == project_id).first()
#     if not db_project:
#         raise HTTPException(status_code=404, detail="Project not found")
#     if current_user not in [db_project.creator] + [c.user for c in db_project.collaborators]:
#         raise HTTPException(status_code=403, detail="Not authorized to upload videos to this project")
    
#     file_path = upload_file(video_file)
#     latest_version = db.query(Video).filter(Video.project_id == project_id).order_by(Video.version.desc()).first()
#     version = 1 if not latest_version else latest_version.version + 1
    
#     db_video = Video(
#         project_id=project_id,
#         name=video.name,
#         description=video.description,
#         version=version,
#         file_path=file_path,
#         uploader_id=current_user.id
#     )
#     db.add(db_video)
#     db.commit()
#     db.refresh(db_video)
#     return db_video

@router.post("/")
def upload_video(project_id: int, video: VideoCreate, video_file: UploadFile = File(...), db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
   
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")
    if current_user not in [db_project.creator] + [c.user for c in db_project.collaborators]:
        raise HTTPException(status_code=403, detail="Not authorized to upload videos to this project")
    
    # Upload the video file to Cloudinary
    file_url = upload_file_to_cloudinary(video_file)
    
    # Create a new video record in the database
    latest_version = db.query(Video).filter(Video.project_id == project_id).order_by(Video.version.desc()).first()
    version = 1 if not latest_version else latest_version.version + 1
    
    db_video = Video(
        project_id=project_id,
        name=video.name,
        description=video.description,
        version=version,
        file_path=file_url,  # Save the Cloudinary URL as the file path
        uploader_id=current_user.id
    )
    db.add(db_video)
    db.commit()
    db.refresh(db_video)
    return db_video


@router.get("/{video_id}")
def get_video(project_id: int, video_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    db_video = db.query(Video).filter(Video.id == video_id, Video.project_id == project_id).first()
    if not db_video:
        raise HTTPException(status_code=404, detail="Video not found")
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if current_user not in [db_project.creator] + [c.user for c in db_project.collaborators]:
        raise HTTPException(status_code=403, detail="Not authorized to access this video")
    return db_video

# @router.delete("/{video_id}", response_model=Video)
# def delete_video(project_id: int, video_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
#     db_video = db.query(Video).filter(Video.id == video_id, Video.project_id == project_id).first()
#     if not db_video:
#         raise HTTPException(status_code=404, detail="Video not found")
#     db_project = db.query(Project).filter(Project.id == project_id).first()
#     if db_project.creator_id != current_user.id:
#         raise HTTPException(status_code=403, detail="Not authorized to delete this video")
    
#     delete_file(db_video.file_path)
#     db.delete(db_video)
#     db.commit()
#     return db_video