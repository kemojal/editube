"""Shared video JSON payloads for list/detail routes."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db.models import Annotation, Comment, Project, Video, VideoTranscription
from app.services.project_access import can_moderate_video_comments


def _comment_row_visible_to_viewer(
    comment: Comment, viewer_user_id: int, by_id: dict[int, Comment]
) -> bool:
    """Match list API: private top-level = author only; private reply = author or parent author."""
    if not getattr(comment, "is_private", False):
        return True
    if comment.user_id == viewer_user_id:
        return True
    pid = comment.parent_id
    if pid is not None:
        parent = by_id.get(pid)
        if parent is not None and parent.user_id == viewer_user_id:
            return True
    return False


def _annotation_visible_to_viewer(annotation: Annotation, viewer_user_id: int) -> bool:
    if not getattr(annotation, "is_private", False):
        return True
    return annotation.user_id == viewer_user_id


def transcription_to_dict(tr: VideoTranscription | None) -> dict[str, Any] | None:
    if tr is None:
        return None
    # JSON clients: use [] instead of null when no transcript has been persisted yet.
    segments = tr.segments if tr.segments is not None else []
    speakers = tr.speakers if tr.speakers is not None else []
    return {
        "status": tr.status,
        "segments": segments,
        "speakers": speakers,
        "speaker_count": tr.speaker_count if tr.speaker_count is not None else 0,
        "error_message": tr.error_message,
        "updated_at": tr.updated_at,
    }


def video_detail_dict(
    video: Video,
    viewer_user_id: int | None = None,
    *,
    db: Session | None = None,
    db_project: Project | None = None,
) -> dict[str, Any]:
    comments = video.comments or []
    annotations = video.annotations or []
    if viewer_user_id is not None:
        by_id = {c.id: c for c in comments}
        comments_count = sum(
            1 for c in comments if _comment_row_visible_to_viewer(c, viewer_user_id, by_id)
        )
        annotations_count = sum(
            1 for a in annotations if _annotation_visible_to_viewer(a, viewer_user_id)
        )
    else:
        comments_count = len(comments)
        annotations_count = len(annotations)
    can_moderate = False
    if (
        viewer_user_id is not None
        and db is not None
        and db_project is not None
    ):
        can_moderate = can_moderate_video_comments(db, db_project, viewer_user_id)
    return {
        "id": video.id,
        "project_id": video.project_id,
        "folder_id": video.folder_id,
        "name": video.name,
        "description": video.description,
        "version": video.version,
        "file_path": video.file_path,
        "thumbnail_url": video.thumbnail_url,
        "status": video.status or "in_progress",
        "duration": video.duration,
        "uploader": video.uploader,
        "created_at": video.created_at,
        "updated_at": video.updated_at,
        "comments_count": comments_count,
        "annotations_count": annotations_count,
        "transcription": transcription_to_dict(video.transcription),
        "can_moderate": can_moderate,
    }
