"""Version-chain helpers shared by the multipart upload route (`version_of`)
and anything else that registers a new `Video` row as the next version of an
existing one (e.g. rough-cut export "save as new version").

There is exactly ONE implementation of the version_group_id/version-number
math: `resolve_version_chain`. The upload route and `register_video_version`
both call it so the semantics can never drift apart.
"""

from __future__ import annotations

import logging
import uuid as _uuid
from typing import Optional

from sqlalchemy.orm import Session

from app.db.models import Video
from app.services.activity import log_activity

logger = logging.getLogger(__name__)


def resolve_version_chain(db: Session, base_video: Video) -> tuple[str, int]:
    """Resolve the (version_group_id, version) pair for the NEXT version
    after `base_video`.

    Inherits `base_video.version_group_id`, backfilling it in-place (mutates
    the ORM object; caller's session flush/commit persists it) if it predates
    the column. `version` is `max(existing versions in that group) + 1`,
    starting at 1 for a freshly-backfilled group.

    Identical to the version_of resolution previously inlined in
    `POST /projects/{project_id}/videos` (upload_video).
    """
    version_group_id = base_video.version_group_id or _uuid.uuid4().hex
    if not base_video.version_group_id:
        base_video.version_group_id = version_group_id

    latest_in_group = (
        db.query(Video)
        .filter(
            Video.project_id == base_video.project_id,
            Video.version_group_id == version_group_id,
        )
        .order_by(Video.version.desc())
        .first()
    )
    version = 1 if not latest_in_group else (latest_in_group.version or 0) + 1
    return version_group_id, version


def register_video_version(
    db: Session,
    source_video: Video,
    *,
    name: str,
    file_path: str,
    size_bytes: Optional[int] = None,
    thumbnail_url: Optional[str] = None,
) -> Video:
    """Create a new `Video` row as the next version of `source_video`'s chain.

    Same project/folder as `source_video`; version_group_id inherited (or
    backfilled) and version = max(existing in group) + 1 via
    `resolve_version_chain` — the same math the multipart upload route's
    `version_of` path uses.

    Deliberately does NOT seed/enqueue a transcription row: this helper is
    used by rough-cut export ("save as new version") to register an edited
    output without immediately re-transcribing it. Callers that want a
    transcription (the primary upload route) enqueue it themselves via
    `_finalize_project_video`. Transcription-on-export can become opt-in
    later without touching this helper's contract.
    """
    version_group_id, version = resolve_version_chain(db, source_video)

    db_video = Video(
        project_id=source_video.project_id,
        folder_id=source_video.folder_id,
        name=name,
        version=version,
        version_group_id=version_group_id,
        file_path=file_path,
        size_bytes=size_bytes or 0,
        thumbnail_url=thumbnail_url,
        uploader_id=source_video.uploader_id,
    )
    db.add(db_video)
    db.flush()

    # Same pattern as _finalize_project_video's log_activity call: log_activity
    # itself is non-fatal (internal try/except), so no extra wrapping needed here.
    log_activity(
        db,
        user_id=source_video.uploader_id,
        project_id=source_video.project_id,
        action="video_version_registered",
        meta={
            "video_name": name,
            "video_id": db_video.id,
            "source_video_id": source_video.id,
            "version": version,
        },
    )

    # Best-effort poster thumbnail, same pattern as _finalize_project_video —
    # skip if the caller already resolved one.
    if not thumbnail_url:
        try:
            from app.jobs.queue import enqueue_video_thumbnail_job

            enqueue_video_thumbnail_job(db_video.id)
        except Exception as e:  # noqa: BLE001
            logger.warning("Thumbnail job not enqueued for version video %s: %s", db_video.id, e)

    # auto_proxy_on_upload is deliberately NOT triggered here (unlike
    # _finalize_project_video): rough-cut exports are already rendered,
    # playback-ready mp4s, so there's nothing to proxy.

    return db_video
