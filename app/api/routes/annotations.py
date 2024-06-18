from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.db.models import Annotation, Video, User
from app.db.database import get_db
from app.utils.security import get_current_user
from app.api.models.annotations import AnnotationCreate, AnnotationUpdate, AnnotationResponse
from app.api.models.users import UserResponse
from typing import List
from datetime import datetime

router = APIRouter(
    prefix="/annotations",
    tags=["Annotations"],
)

@router.post("/{video_id}", response_model=AnnotationResponse)
def create_annotation(
    video_id: int,
    annotation: AnnotationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    db_video = db.query(Video).filter(Video.id == video_id).first()
    if not db_video:
        raise HTTPException(status_code=404, detail="Video not found")
    
    if current_user not in [db_video.project.creator] + [c.user for c in db_video.project.collaborators]:
        raise HTTPException(status_code=403, detail="Not authorized to annotate this video")

    # Create a new Annotation instance
    db_annotation = Annotation(
        video_id=video_id,
        user_id=current_user.id,
        annotation_type=annotation.annotation_type,
        annotation_data=annotation.annotation_data,
        timecode=annotation.timecode,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )

    db.add(db_annotation)
    db.commit()
    db.refresh(db_annotation)

    # Return the created annotation as AnnotationResponse
    return AnnotationResponse.from_orm(db_annotation)

@router.get("/{video_id}", response_model=List[AnnotationResponse])
def get_video_annotations(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    # Fetch the video from the database
    db_video = db.query(Video).filter(Video.id == video_id).first()
    if not db_video:
        raise HTTPException(status_code=404, detail="Video not found")
    
    # Check if the current user is authorized to access annotations for this video
    authorized_users = [db_video.project.creator] + [c.user for c in db_video.project.collaborators]
    if current_user not in authorized_users:
        raise HTTPException(status_code=403, detail="Not authorized to access annotations for this video")
    
    # Fetch annotations for the given video ID
    annotations = db.query(Annotation).filter(Annotation.video_id == video_id).all()
    
    # Convert SQLAlchemy objects to Pydantic models (AnnotationResponse)
    # annotation_responses = [
    #     AnnotationResponse(
    #         id=annotation.id,
    #         video_id=annotation.video_id,
    #         # user_id=annotation.user_id,
    #         user=UserResponse(
    #             name=annotation.user.name,
    #             email=annotation.user.email,
    #             role=annotation.user.role
    #         ),
    #         # text=annotation.text,
    #         annotation_type=annotation.annotation_type,
    #         annotation_data=annotation.annotation_data,
    #         timecode=str(annotation.timecode),  # Adjust if necessary
    #         created_at=annotation.created_at,
    #         updated_at=annotation.updated_at
    #     )
    #     for annotation in annotations
    # ]
#     annotation_responses = [
#     AnnotationResponse(
#         id=annotation.id,
#         video_id=annotation.video_id,
#         user=UserResponse(
#             id=annotation.user.id,
#             name=annotation.user.name,
#             email=annotation.user.email,
#             role=annotation.user.role
#         ),
#         annotation_type=annotation.annotation_type,
#         annotation_data=annotation.annotation_data,
#         timecode=str(annotation.timecode),
#         created_at=annotation.created_at,
#         updated_at=annotation.updated_at
#     )
#     for annotation in annotations
# ]
    annotation_responses = [
        AnnotationResponse(
            id=annotation.id,
            video_id=annotation.video_id,
            user=UserResponse(
                id=annotation.user.id,
                name=annotation.user.name,
                email=annotation.user.email,
                role=annotation.user.role
            ),
            annotation_data=annotation.annotation_data,
            annotation_type=annotation.annotation_type,
            timecode=str(annotation.timecode),
            created_at=annotation.created_at,
            updated_at=annotation.updated_at
        )
        for annotation in annotations
    ]
    
    return annotation_responses


@router.put("/{annotation_id}", response_model=AnnotationResponse)
def update_annotation(
    annotation_id: int,
    annotation: AnnotationUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    db_annotation = db.query(Annotation).filter(Annotation.id == annotation_id).first()
    if not db_annotation:
        raise HTTPException(status_code=404, detail="Annotation not found")
    
    db_video = db.query(Video).filter(Video.id == db_annotation.video_id).first()
    if current_user.id != db_annotation.user_id and current_user.id != db_video.uploader_id:
        raise HTTPException(status_code=403, detail="Not authorized to update this annotation")
    
    update_data = annotation.dict(exclude_unset=True)
    for field, value in update_data.items():
        setattr(db_annotation, field, value)
    
    db.commit()
    db.refresh(db_annotation)
    
    return AnnotationResponse.from_orm(db_annotation)

@router.delete("/{annotation_id}")
def delete_annotation(
    annotation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    db_annotation = db.query(Annotation).filter(Annotation.id == annotation_id).first()
    if not db_annotation:
        raise HTTPException(status_code=404, detail="Annotation not found")
    
    db_video = db.query(Video).filter(Video.id == db_annotation.video_id).first()
    if current_user.id != db_annotation.user_id and current_user.id != db_video.uploader_id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this annotation")
    
    db.delete(db_annotation)
    db.commit()
    
    return {"message": "Annotation deleted successfully"}