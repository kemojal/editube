"""Shared video JSON payloads for list/detail routes."""

from __future__ import annotations

from typing import Any

from app.db.models import Video, VideoTranscription


def transcription_to_dict(tr: VideoTranscription | None) -> dict[str, Any] | None:
    if tr is None:
        return None
    return {
        "status": tr.status,
        "segments": tr.segments,
        "error_message": tr.error_message,
    }


def video_detail_dict(video: Video) -> dict[str, Any]:
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
        "comments_count": len(video.comments) if video.comments else 0,
        "annotations_count": len(video.annotations) if video.annotations else 0,
        "transcription": transcription_to_dict(video.transcription),
    }
