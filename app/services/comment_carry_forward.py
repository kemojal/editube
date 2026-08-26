"""Carry an editor's punch list onto the next version.

Comments bind to a single `video_id`, and a new version is a new `Video` row.
That meant every unresolved change request became read-only history the moment
v2 landed: the editor's checklist evaporated at exactly the point they sat down
to work through it, and the only trace was the `?include_prior=true` view,
which is deliberately not actionable.

Carry-forward **copies**, it never moves. The original stays on the version
where it was raised — that is what the history view is for, and it keeps the
operation reversible (delete the copies and nothing is lost). The copy points
home through `carried_from_comment_id`.
"""

from __future__ import annotations

import logging
from typing import Iterable

from sqlalchemy.orm import Session

from app.db.models import Comment, Video
from app.services.comment_workflow import (
    COMMENT_KIND_CHANGE_REQUEST,
    TERMINAL_STATUSES,
    sync_is_resolved_from_status,
)

logger = logging.getLogger(__name__)


def open_change_requests(db: Session, video_id: int) -> list[Comment]:
    """Top-level change requests on a version that nobody has closed.

    Replies are excluded: a thread belongs to the version where the
    conversation happened. The copy links back to it, so the discussion is one
    click away rather than duplicated out of context.
    """
    return (
        db.query(Comment)
        .filter(
            Comment.video_id == video_id,
            Comment.kind == COMMENT_KIND_CHANGE_REQUEST,
            Comment.parent_id.is_(None),
            Comment.status.notin_(list(TERMINAL_STATUSES)),
        )
        .order_by(Comment.timecode.asc(), Comment.id.asc())
        .all()
    )


def count_open_change_requests(db: Session, video_id: int) -> int:
    """What the upload dialog promises before the editor commits to it."""
    return len(open_change_requests(db, video_id))


def _already_carried(db: Session, target_video_id: int, source_ids: Iterable[int]) -> set[int]:
    source_ids = list(source_ids)
    if not source_ids:
        return set()
    rows = (
        db.query(Comment.carried_from_comment_id)
        .filter(
            Comment.video_id == target_video_id,
            Comment.carried_from_comment_id.in_(source_ids),
        )
        .all()
    )
    return {row[0] for row in rows if row[0] is not None}


def carry_forward_open_change_requests(
    db: Session,
    source_video: Video,
    target_video: Video,
) -> list[Comment]:
    """Copy every open change request from `source_video` onto `target_video`.

    Idempotent: running twice does not duplicate, because each copy records the
    comment it came from. Does not commit — the caller owns the transaction.
    """
    if source_video is None or target_video is None:
        return []
    if source_video.id == target_video.id:
        return []

    originals = open_change_requests(db, source_video.id)
    if not originals:
        return []

    skip = _already_carried(db, target_video.id, [c.id for c in originals])

    copies: list[Comment] = []
    for original in originals:
        if original.id in skip:
            continue
        copy = Comment(
            video_id=target_video.id,
            user_id=original.user_id,
            parent_id=None,
            text=original.text,
            timecode=original.timecode,
            end_timecode=original.end_timecode,
            drawing_data=original.drawing_data,
            # Word indices are dropped deliberately: they point into the old
            # cut's transcript. `anchor_text` survives so the existing
            # anchor-remap logic can re-resolve it against the new one, or flag
            # drift if the line is gone.
            anchor_text=original.anchor_text,
            kind=COMMENT_KIND_CHANGE_REQUEST,
            # Reopened on the new cut regardless of where it had got to on the
            # old one — "in progress" against a version that no longer exists
            # would be a lie.
            status="open",
            visibility=original.visibility,
            is_private=original.is_private,
            assignee_user_id=original.assignee_user_id,
            due_at=original.due_at,
            # Guest authorship survives, so the client's name stays on their
            # own request rather than the request appearing to be the editor's.
            guest_name=original.guest_name,
            guest_email=original.guest_email,
            guest_avatar_url=original.guest_avatar_url,
            review_link_id=original.review_link_id,
            carried_from_comment_id=original.id,
        )
        sync_is_resolved_from_status(copy)
        db.add(copy)
        copies.append(copy)

    if copies:
        db.flush()
        logger.info(
            "Carried %s open change request(s) from video %s to %s",
            len(copies),
            source_video.id,
            target_video.id,
        )
    return copies
