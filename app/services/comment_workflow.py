"""Comment kind/status and review approval gate helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import Comment, ReviewLink, ReviewSession, ReviewWorkflowRun, ReviewWorkflowStage

COMMENT_KIND_COMMENT = "comment"
COMMENT_KIND_CHANGE_REQUEST = "change_request"

COMMENT_STATUS_OPEN = "open"
COMMENT_STATUS_IN_PROGRESS = "in_progress"
COMMENT_STATUS_RESOLVED = "resolved"
COMMENT_STATUS_WONTFIX = "wontfix"
COMMENT_STATUS_REOPENED = "reopened"

TERMINAL_STATUSES = frozenset({COMMENT_STATUS_RESOLVED, COMMENT_STATUS_WONTFIX})


def sync_is_resolved_from_status(comment: Comment) -> None:
    comment.is_resolved = comment.status in TERMINAL_STATUSES


def apply_status(comment: Comment, status: str) -> None:
    comment.status = status
    comment.status_changed_at = datetime.now(timezone.utc)
    sync_is_resolved_from_status(comment)


def unresolved_change_requests_for_link(db: Session, review_link_id: int, video_id: int) -> int:
    return (
        db.query(Comment.id)
        .filter(
            Comment.video_id == video_id,
            Comment.review_link_id == review_link_id,
            Comment.kind == COMMENT_KIND_CHANGE_REQUEST,
            Comment.parent_id.is_(None),
            Comment.status.notin_(list(TERMINAL_STATUSES)),
        )
        .count()
    )


def workflow_incomplete(db: Session, link: ReviewLink) -> bool:
    run = (
        db.query(ReviewWorkflowRun)
        .filter(ReviewWorkflowRun.review_link_id == link.id)
        .first()
    )
    if not run:
        return False
    return run.completed_at is None


def ordered_workflow_stages(db: Session, template_id: int) -> list[ReviewWorkflowStage]:
    return (
        db.query(ReviewWorkflowStage)
        .filter(ReviewWorkflowStage.template_id == template_id)
        .order_by(ReviewWorkflowStage.stage_index.asc())
        .all()
    )


def _notify_ids(stage: ReviewWorkflowStage | None) -> list[int]:
    if not stage:
        return []
    raw = stage.notify_user_ids or []
    if isinstance(raw, list):
        out: list[int] = []
        for x in raw:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                continue
        return out
    return []


def notify_user_ids_for_new_run(db: Session, run: ReviewWorkflowRun) -> list[int]:
    """When a run is created, ping the first stage's recipients."""
    stages = ordered_workflow_stages(db, run.template_id)
    if not stages:
        return []
    return _notify_ids(stages[0])


def advance_workflow_run(db: Session, run: ReviewWorkflowRun) -> list[int]:
    """Mark current stage complete; return user ids to notify for the next active stage (if any)."""
    stages = ordered_workflow_stages(db, run.template_id)
    if not stages:
        run.completed_at = datetime.now(timezone.utc)
        run.current_stage_index = 0
        return []

    # current_stage_index = count of completed stages (0..len)
    completed = run.current_stage_index
    if completed >= len(stages):
        run.completed_at = datetime.now(timezone.utc)
        return []

    completed += 1
    run.current_stage_index = completed
    if completed >= len(stages):
        run.completed_at = datetime.now(timezone.utc)
        return []
    return _notify_ids(stages[completed])


def client_approve_blockers(db: Session, link: ReviewLink) -> list[dict[str, Any]]:
    """Reasons the guest cannot POST /approve (workflow + change requests only)."""
    reasons: list[dict[str, Any]] = []
    if workflow_incomplete(db, link):
        reasons.append(
            {
                "code": "workflow_incomplete",
                "message": "Internal approval stages are not complete for this review link.",
            }
        )
    n_cr = unresolved_change_requests_for_link(db, link.id, link.video_id)
    if n_cr > 0:
        reasons.append(
            {
                "code": "unresolved_change_requests",
                "message": f"{n_cr} change request(s) must be resolved or marked won't-fix before approval.",
                "count": n_cr,
            }
        )
    return reasons


def download_blockers(
    db: Session,
    link: ReviewLink,
    session: ReviewSession | None,
) -> list[dict[str, Any]]:
    """Extra reasons download is still locked (after allow_download + deliverables checks)."""
    reasons = client_approve_blockers(db, link)
    if link.approval_required_for_download and (not session or not session.approved_at):
        reasons.append(
            {
                "code": "approval_required",
                "message": "Client approval is required before download unlocks.",
            }
        )
    return reasons
