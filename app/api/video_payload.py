"""Shared video JSON payloads for list/detail routes."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased, joinedload

from app.db.models import Annotation, Comment, Project, Video, VideoTranscription
from app.services.project_access import can_moderate_video_comments
from app.services.video_status import decision_summary, normalize_status


def _visible_counts_sql(
    db: Session, video_id: int, viewer_user_id: int | None
) -> tuple[int, int]:
    """Comment/annotation counts for the detail payload, in one round trip.

    Mirrors _comment_row_visible_to_viewer / _annotation_visible_to_viewer:
    private top-level = author only; private reply = author or parent author;
    private annotation = author only. Replaces loading every comment and
    annotation row (FabricJS/JSONB blobs included) just to count them.
    """
    parent = aliased(Comment)
    comments_q = (
        select(func.count(Comment.id))
        .outerjoin(parent, Comment.parent_id == parent.id)
        .where(Comment.video_id == video_id)
    )
    annotations_q = select(func.count(Annotation.id)).where(
        Annotation.video_id == video_id
    )
    if viewer_user_id is not None:
        comments_q = comments_q.where(
            or_(
                Comment.is_private.is_(False),
                Comment.user_id == viewer_user_id,
                parent.user_id == viewer_user_id,
            )
        )
        annotations_q = annotations_q.where(
            or_(
                Annotation.is_private.is_(False),
                Annotation.user_id == viewer_user_id,
            )
        )
    row = db.execute(
        select(
            comments_q.scalar_subquery().label("comments"),
            annotations_q.scalar_subquery().label("annotations"),
        )
    ).one()
    return int(row.comments or 0), int(row.annotations or 0)


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
        "audio_analysis": getattr(tr, "audio_analysis", None),
        "error_message": tr.error_message,
        "updated_at": tr.updated_at,
        "language": tr.language,
        "detected_language": tr.detected_language,
    }


def video_versions_payload(db: Session, video: Video) -> list[dict[str, Any]]:
    """versions[] for the player's version switcher (VideoVersionSummary).

    For Review projects (project_type == "review"), all videos in the project
    count as versions of the deliverable (version control flow). For other
    workflows, siblings in this video's chain (same version_group_id) count as
    versions. Newest first, with per-version comment counts batched in one query.
    """
    project_type = None
    if getattr(video, "project", None) is not None:
        project_type = video.project.project_type
    elif video.project_id:
        proj = db.query(Project.project_type).filter(Project.id == video.project_id).first()
        if proj:
            project_type = proj[0]

    if project_type == "review":
        versions = (
            db.query(Video)
            .options(joinedload(Video.uploader))
            .filter(Video.project_id == video.project_id)
            .order_by(Video.version.desc(), Video.id.desc())
            .all()
        )
    elif video.version_group_id:
        versions = (
            db.query(Video)
            .options(joinedload(Video.uploader))
            .filter(
                Video.project_id == video.project_id,
                Video.version_group_id == video.version_group_id,
            )
            .order_by(Video.version.desc())
            .all()
        )
    else:
        versions = [video]

    version_ids = [v.id for v in versions]
    comment_counts: dict[int, int] = {}
    if version_ids:
        rows = (
            db.query(Comment.video_id, func.count(Comment.id))
            .filter(Comment.video_id.in_(version_ids))
            .group_by(Comment.video_id)
            .all()
        )
        comment_counts = {vid: int(cnt) for vid, cnt in rows}

    return [
        {
            "id": v.id,
            "version": v.version,
            "name": v.name,
            "created_at": v.created_at,
            "thumbnail_url": v.thumbnail_url,
            "file_path": v.file_path,
            "duration": v.duration,
            "comment_count": comment_counts.get(v.id, 0),
            "uploader_name": v.uploader.name if v.uploader else None,
            "status": normalize_status(v.status),
        }
        for v in versions
    ]


def video_detail_dict(
    video: Video,
    viewer_user_id: int | None = None,
    *,
    db: Session | None = None,
    db_project: Project | None = None,
) -> dict[str, Any]:
    if db is not None:
        comments_count, annotations_count = _visible_counts_sql(
            db, video.id, viewer_user_id
        )
    else:
        # No session: fall back to counting whatever the caller loaded.
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

    # Editing-proxy rendition, so the editor can play a scrub-friendly file
    # instead of the full-resolution master. Status lets the client keep
    # polling/hot-swap once generation completes.
    proxy_url = None
    proxy_status = None
    if db is not None:
        from app.services.proxy_service import DEFAULT_PROFILE, get_proxy

        proxy = get_proxy(db, video.id, DEFAULT_PROFILE)
        if proxy is not None:
            proxy_status = proxy.status
            if proxy.status == "completed":
                proxy_url = proxy.file_url

    return {
        "id": video.id,
        "project_id": video.project_id,
        "folder_id": video.folder_id,
        "name": video.name,
        "description": video.description,
        "version": video.version,
        "file_path": video.file_path,
        "thumbnail_url": video.thumbnail_url,
        # Normalized, so the UI never offers actions the service would refuse:
        # legacy rows still hold values like 'ready'.
        "status": normalize_status(video.status),
        "status_changed_at": getattr(video, "status_changed_at", None),
        "review_due_at": getattr(video, "review_due_at", None),
        "version_notes": getattr(video, "version_notes", None),
        "decision": decision_summary(db, video) if db is not None else None,
        "duration": video.duration,
        "uploader": video.uploader,
        "created_at": video.created_at,
        "updated_at": video.updated_at,
        "comments_count": comments_count,
        "annotations_count": annotations_count,
        "transcription": transcription_to_dict(video.transcription),
        "can_moderate": can_moderate,
        "proxy_url": proxy_url,
        "proxy_status": proxy_status,
    }
