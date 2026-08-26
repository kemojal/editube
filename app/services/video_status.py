"""Video review status: the vocabulary, the legal moves, and the one writer.

Before this module, `Video.status` was assigned inline in two near-duplicate
route handlers (`app/api/routes/videos.py` and `app/api/routes/video_detail.py`),
each carrying its own copy of the valid-status tuple, neither recording who
changed it or when, and neither checking that the move made sense — a cut could
go straight from `in_progress` to `approved` without ever having been reviewed.

Everything that changes a video's review state now goes through
`apply_video_status`. Callers own the transaction (this module flushes, never
commits), matching the convention in `app/services/video_versions.py`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import Video, VideoApproval
from app.services.activity import log_activity

logger = logging.getLogger(__name__)

STATUS_IN_PROGRESS = "in_progress"
STATUS_IN_REVIEW = "in_review"
STATUS_APPROVED = "approved"
STATUS_NEEDS_CHANGES = "needs_changes"

VIDEO_STATUSES: tuple[str, ...] = (
    STATUS_IN_PROGRESS,
    STATUS_IN_REVIEW,
    STATUS_APPROVED,
    STATUS_NEEDS_CHANGES,
)

# Human-readable labels, mirrored by `lib/review/status.ts` on the frontend.
STATUS_LABELS: dict[str, str] = {
    STATUS_IN_PROGRESS: "In progress",
    STATUS_IN_REVIEW: "In review",
    STATUS_APPROVED: "Approved",
    STATUS_NEEDS_CHANGES: "Changes requested",
}

# Legal moves. Permissive inside the review loop, strict about nonsense.
#
# `approved -> in_review` and `approved -> needs_changes` are both allowed
# because clients change their minds after signing off, and the product should
# model that rather than force a fake new version to express it.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_IN_PROGRESS: frozenset({STATUS_IN_REVIEW}),
    STATUS_IN_REVIEW: frozenset({STATUS_APPROVED, STATUS_NEEDS_CHANGES, STATUS_IN_PROGRESS}),
    STATUS_NEEDS_CHANGES: frozenset({STATUS_IN_REVIEW, STATUS_IN_PROGRESS}),
    STATUS_APPROVED: frozenset({STATUS_IN_REVIEW, STATUS_NEEDS_CHANGES}),
}


class InvalidVideoStatus(ValueError):
    """Raised for a status outside the vocabulary."""


class IllegalStatusTransition(ValueError):
    """Raised for a status that exists but cannot be reached from here."""

    def __init__(self, current: str, target: str) -> None:
        self.current = current
        self.target = target
        allowed = sorted(ALLOWED_TRANSITIONS.get(current, frozenset()))
        super().__init__(
            f"Cannot move a video from '{STATUS_LABELS.get(current, current)}' to "
            f"'{STATUS_LABELS.get(target, target)}'. "
            + (f"Allowed from here: {', '.join(allowed)}." if allowed else "No moves are allowed from here.")
        )


def assert_valid_status(status: str) -> str:
    if status not in VIDEO_STATUSES:
        raise InvalidVideoStatus(
            f"Invalid status '{status}'. Must be one of: {', '.join(VIDEO_STATUSES)}"
        )
    return status


def normalize_status(status: str | None) -> str:
    """Coerce a stored status into the current vocabulary.

    The column has carried other values over the product's life — real rows
    still hold `'ready'` from an earlier upload pipeline — and a value outside
    `ALLOWED_TRANSITIONS` has no legal moves at all, so those videos could
    never be sent for review: every transition raised. Reading them as
    `in_progress` (the column's server default, and what "ready to work on"
    meant) puts them back in the flow.

    Applied on read and before every transition, so nothing has to be
    rewritten in the database and any future stray value degrades the same
    safe way rather than deadlocking a cut.
    """
    return status if status in VIDEO_STATUSES else STATUS_IN_PROGRESS


def assert_transition(current: str | None, target: str) -> None:
    """Check a move is legal. Unknown or missing current statuses are read as
    `in_progress` — see `normalize_status`."""
    assert_valid_status(target)
    current = normalize_status(current)
    if current == target:
        return
    if target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise IllegalStatusTransition(current, target)


def apply_video_status(
    db: Session,
    video: Video,
    target: str,
    *,
    actor_user_id: int | None = None,
    note: str | None = None,
    skip_transition_check: bool = False,
) -> Video:
    """Move a video to `target`, recording who did it and when.

    `skip_transition_check` exists for one caller: registering a new version,
    which resets the cut to `in_review` regardless of where its predecessor
    sat. Every other path should let the transition rules apply.

    Does not commit — the caller owns the transaction.
    """
    assert_valid_status(target)
    current = normalize_status(video.status)
    if not skip_transition_check:
        assert_transition(current, target)

    if current == target:
        return video

    video.status = target
    video.status_changed_at = datetime.now(timezone.utc)
    video.status_changed_by = actor_user_id

    if video.project_id and actor_user_id:
        log_activity(
            db,
            user_id=actor_user_id,
            project_id=video.project_id,
            action="video_status_changed",
            meta={
                "video_id": video.id,
                "video_name": video.name,
                "from": current,
                "to": target,
                "note": (note or "")[:280] or None,
            },
        )

    db.flush()
    return video


def is_awaiting_review(video: Video) -> bool:
    return normalize_status(video.status) == STATUS_IN_REVIEW


def is_approved(video: Video) -> bool:
    return normalize_status(video.status) == STATUS_APPROVED


# --- Decisions ---------------------------------------------------------------

DECISION_APPROVED = "approved"
DECISION_CHANGES_REQUESTED = "changes_requested"

DECISIONS: tuple[str, ...] = (DECISION_APPROVED, DECISION_CHANGES_REQUESTED)

# A decision implies where the cut lands. Keeping this mapping here — rather
# than at the two call sites — is what stops the guest path and the team path
# from ever disagreeing about what "approved" means.
_STATUS_FOR_DECISION: dict[str, str] = {
    DECISION_APPROVED: STATUS_APPROVED,
    DECISION_CHANGES_REQUESTED: STATUS_NEEDS_CHANGES,
}


def record_decision(
    db: Session,
    video: Video,
    decision: str,
    *,
    actor_user_id: int | None = None,
    review_session_id: int | None = None,
    review_link_id: int | None = None,
    note: str | None = None,
) -> VideoApproval:
    """Append a review decision and move the video's status to match.

    Used identically by the authenticated route and the guest review link.
    Does not commit — the caller owns the transaction.
    """
    if decision not in DECISIONS:
        raise ValueError(
            f"Invalid decision '{decision}'. Must be one of: {', '.join(DECISIONS)}"
        )

    approval = VideoApproval(
        video_id=video.id,
        decision=decision,
        actor_user_id=actor_user_id,
        review_session_id=review_session_id,
        review_link_id=review_link_id,
        note=note,
    )
    db.add(approval)

    apply_video_status(
        db,
        video,
        _STATUS_FOR_DECISION[decision],
        actor_user_id=actor_user_id,
        note=note,
        # A guest approving a cut that is still `in_progress` (because the
        # editor shared a link without formally sending it for review) is a
        # real sequence, and refusing it would strand them.
        skip_transition_check=True,
    )
    db.flush()
    return approval


def supersede_open_decisions(
    db: Session,
    video_ids: list[int],
    *,
    superseded_by_video_id: int,
) -> int:
    """Mark every live decision on these versions as superseded.

    Called when a new version lands, so "client approved v2" can never be
    misread as "the current cut is approved".
    """
    if not video_ids:
        return 0
    now = datetime.now(timezone.utc)
    updated = (
        db.query(VideoApproval)
        .filter(
            VideoApproval.video_id.in_(video_ids),
            VideoApproval.superseded_at.is_(None),
        )
        .update(
            {
                VideoApproval.superseded_at: now,
                VideoApproval.superseded_by_video_id: superseded_by_video_id,
            },
            synchronize_session=False,
        )
    )
    db.flush()
    return int(updated or 0)


def latest_decision(db: Session, video: Video) -> VideoApproval | None:
    """The most recent decision on this version, superseded or not."""
    return (
        db.query(VideoApproval)
        .filter(VideoApproval.video_id == video.id)
        .order_by(VideoApproval.created_at.desc(), VideoApproval.id.desc())
        .first()
    )


def decision_summary(db: Session, video: Video) -> dict | None:
    """What the header badge and version switcher render, or None if this cut
    has never been decided on."""
    approval = latest_decision(db, video)
    if approval is None:
        return None

    actor_name = None
    if approval.actor is not None:
        actor_name = approval.actor.name or approval.actor.email
    elif approval.review_session is not None:
        actor_name = (
            approval.review_session.guest_name
            or approval.review_session.guest_email
            or "Guest reviewer"
        )

    return {
        "decision": approval.decision,
        "actor_name": actor_name,
        "note": approval.note,
        "created_at": approval.created_at,
        "superseded": approval.superseded_at is not None,
    }
