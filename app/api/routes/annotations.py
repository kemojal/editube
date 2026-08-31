from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload
from app.db.models import Annotation, Video, User
from app.services.project_access import assert_write_project_content, can_access_project
from app.db.database import get_db
from app.utils.security import get_current_user
from app.services.product_analytics import emit_once
from app.api.models.annotations import (
    ANNOTATION_DURATION_FRAMES,
    AnnotationCreate,
    AnnotationUpdate,
    AnnotationResponse,
    AnnotationUserResponse,
)
from typing import List

router = APIRouter(
    prefix="/annotations",
    tags=["Annotations"],
)


def _annotation_visible_to_viewer(annotation: Annotation, viewer_id: int) -> bool:
    if not annotation.is_private:
        return True
    return annotation.user_id == viewer_id


def _annotation_response(a: Annotation) -> dict:
    return {
        "id": a.id,
        "video_id": a.video_id,
        "user": AnnotationUserResponse(
            id=a.user.id,
            name=a.user.name,
            email=a.user.email,
            avatar_url=getattr(a.user, "avatar_url", None),
        ),
        "annotation_type": a.annotation_type,
        "annotation_data": a.annotation_data,
        "timecode": int(a.timecode) if isinstance(a.timecode, str) else a.timecode,
        "duration": a.duration if a.duration else ANNOTATION_DURATION_FRAMES,
        "is_private": a.is_private,
        "created_at": a.created_at,
        "updated_at": a.updated_at,
    }


@router.post("/{video_id}", response_model=AnnotationResponse)
def create_annotation(
    video_id: int,
    annotation: AnnotationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db_video = db.query(Video).filter(Video.id == video_id).first()
    if not db_video:
        raise HTTPException(status_code=404, detail="Video not found")

    if not can_access_project(db, current_user.id, db_video.project):
        raise HTTPException(
            status_code=403, detail="Not authorized to annotate this video"
        )
    assert_write_project_content(db, current_user, db_video.project)

    db_annotation = Annotation(
        video_id=video_id,
        user_id=current_user.id,
        annotation_type=annotation.annotation_type,
        annotation_data=annotation.annotation_data,
        timecode=annotation.timecode,
        duration=annotation.duration if annotation.duration else ANNOTATION_DURATION_FRAMES,
        is_private=annotation.is_private,
    )

    db.add(db_annotation)
    db.flush()
    emit_once(
        db,
        "feature_completed",
        event_id=f"feature:annotations:annotation:{db_annotation.id}:created",
        user=current_user,
        workspace_id=db_video.project.workspace_id if db_video.project else None,
        properties={
            "feature_key": "annotations",
            "project_id": db_video.project_id,
            "video_id": db_video.id,
            "annotation_id": db_annotation.id,
            "completion_type": "annotation_persisted",
            "result": "success",
        },
    )
    db.commit()
    db.refresh(db_annotation)

    return _annotation_response(db_annotation)


@router.get("/{video_id}", response_model=List[AnnotationResponse])
def get_video_annotations(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db_video = db.query(Video).filter(Video.id == video_id).first()
    if not db_video:
        raise HTTPException(status_code=404, detail="Video not found")

    if not can_access_project(db, current_user.id, db_video.project):
        raise HTTPException(
            status_code=403,
            detail="Not authorized to access annotations for this video",
        )

    annotations = (
        db.query(Annotation)
        # The serializer reads a.user for the author block, which was one
        # SELECT per annotation on a route the player calls on every mount.
        .options(joinedload(Annotation.user))
        .filter(Annotation.video_id == video_id)
        .order_by(Annotation.timecode.asc())
        .all()
    )

    return [
        _annotation_response(a)
        for a in annotations
        if _annotation_visible_to_viewer(a, current_user.id)
    ]


@router.put("/{annotation_id}", response_model=AnnotationResponse)
def update_annotation(
    annotation_id: int,
    annotation: AnnotationUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db_annotation = (
        db.query(Annotation).filter(Annotation.id == annotation_id).first()
    )
    if not db_annotation or not _annotation_visible_to_viewer(
        db_annotation, current_user.id
    ):
        raise HTTPException(status_code=404, detail="Annotation not found")

    db_video = db.query(Video).filter(Video.id == db_annotation.video_id).first()
    if not can_access_project(db, current_user.id, db_video.project):
        raise HTTPException(status_code=403, detail="Not authorized")
    if current_user.id != db_annotation.user_id:
        assert_write_project_content(db, current_user, db_video.project)

    update_data = annotation.dict(exclude_unset=True)
    for field, value in update_data.items():
        setattr(db_annotation, field, value)

    emit_once(
        db,
        "feature_result_used",
        event_id=f"feature:annotations:annotation:{db_annotation.id}:first-update",
        user=current_user,
        workspace_id=db_video.project.workspace_id if db_video.project else None,
        properties={
            "feature_key": "annotations",
            "project_id": db_video.project_id,
            "video_id": db_video.id,
            "annotation_id": db_annotation.id,
            "result_action": "annotation_revisited",
            "result": "success",
        },
    )

    db.commit()
    db.refresh(db_annotation)

    return _annotation_response(db_annotation)


@router.delete("/{annotation_id}")
def delete_annotation(
    annotation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db_annotation = (
        db.query(Annotation).filter(Annotation.id == annotation_id).first()
    )
    if not db_annotation or not _annotation_visible_to_viewer(
        db_annotation, current_user.id
    ):
        raise HTTPException(status_code=404, detail="Annotation not found")

    db_video = db.query(Video).filter(Video.id == db_annotation.video_id).first()
    if not can_access_project(db, current_user.id, db_video.project):
        raise HTTPException(status_code=403, detail="Not authorized")
    if current_user.id != db_annotation.user_id:
        assert_write_project_content(db, current_user, db_video.project)

    db.delete(db_annotation)
    db.commit()

    return {"message": "Annotation deleted successfully"}
