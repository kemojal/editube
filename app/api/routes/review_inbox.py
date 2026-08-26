"""The review inbox: "what needs me right now", across every project.

The dashboard answers "what exists". Nothing answered "what is my next
action" — `/dashboard/reviews` redirected straight back to `/dashboard`, and a
creator with thirty videos had no way to find the three waiting on them short
of opening each one.

Every section here is computed from state that already exists. Nothing is
configured, and nothing needs maintaining: a video appears because of what is
true about it, not because someone remembered to file it.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from app.db.database import get_db
from app.db.models import (
    Comment,
    Project,
    ProjectCollaborator,
    ReviewLink,
    ReviewSession,
    User,
    Video,
    VideoApproval,
    WorkspaceMember,
)
from app.services.comment_workflow import (
    COMMENT_KIND_CHANGE_REQUEST,
    TERMINAL_STATUSES,
)
from app.services.video_status import (
    STATUS_APPROVED,
    STATUS_IN_REVIEW,
    STATUS_NEEDS_CHANGES,
)
from app.utils.security import get_current_user

router = APIRouter(prefix="/reviews", tags=["Review inbox"])

# Why a row is in front of you. The UI turns these into its one-line
# explanation, so the reason travels with the data instead of being
# re-derived in the client.
REASON_REVIEW_REQUESTED = "review_requested"
REASON_CHANGES_REQUESTED = "changes_requested"
REASON_ASSIGNED_COMMENTS = "assigned_comments"


def _accessible_project_ids(db: Session, user_id: int) -> list[int]:
    """Projects this user can see, by any of the three routes that grant it.

    Mirrors `can_access_project` but as a single set-returning query — the
    per-project helper would be N queries across an inbox.
    """
    created = db.query(Project.id).filter(Project.creator_id == user_id)
    collaborating = (
        db.query(ProjectCollaborator.project_id)
        .filter(ProjectCollaborator.user_id == user_id)
        .filter(func.lower(func.coalesce(ProjectCollaborator.role, "")) != "client")
    )
    # Workspace members reach every project in the workspace, except clients,
    # who only ever participate through guest review links.
    workspace_ids = [
        row[0]
        for row in db.query(WorkspaceMember.workspace_id)
        .filter(
            WorkspaceMember.user_id == user_id,
            WorkspaceMember.role.notin_(["client", "guest"]),
        )
        .all()
    ]
    via_workspace = (
        db.query(Project.id).filter(Project.workspace_id.in_(workspace_ids))
        if workspace_ids
        else None
    )

    ids: set[int] = {row[0] for row in created.all()}
    ids.update(row[0] for row in collaborating.all())
    if via_workspace is not None:
        ids.update(row[0] for row in via_workspace.all())
    return sorted(ids)


def _open_counts(db: Session, video_ids: list[int]) -> tuple[dict, dict]:
    """Open comment and open change-request counts, batched."""
    if not video_ids:
        return {}, {}

    open_comments = dict(
        db.query(Comment.video_id, func.count(Comment.id))
        .filter(
            Comment.video_id.in_(video_ids),
            Comment.parent_id.is_(None),
            Comment.status.notin_(list(TERMINAL_STATUSES)),
        )
        .group_by(Comment.video_id)
        .all()
    )
    open_crs = dict(
        db.query(Comment.video_id, func.count(Comment.id))
        .filter(
            Comment.video_id.in_(video_ids),
            Comment.parent_id.is_(None),
            Comment.kind == COMMENT_KIND_CHANGE_REQUEST,
            Comment.status.notin_(list(TERMINAL_STATUSES)),
        )
        .group_by(Comment.video_id)
        .all()
    )
    return open_comments, open_crs


def _row(
    video: Video,
    project: Project | None,
    *,
    reason: str,
    open_comments: int = 0,
    open_change_requests: int = 0,
    extra: dict | None = None,
) -> dict:
    payload = {
        "video_id": video.id,
        "name": video.name,
        "version": video.version or 1,
        "thumbnail_url": video.thumbnail_url,
        "status": video.status or "in_progress",
        "project_id": video.project_id,
        "project_name": project.name if project else None,
        "client_name": getattr(project, "client_name", None) if project else None,
        "review_due_at": video.review_due_at,
        "status_changed_at": video.status_changed_at,
        "open_comments": open_comments,
        "open_change_requests": open_change_requests,
        "reason": reason,
    }
    if extra:
        payload.update(extra)
    return payload


@router.get("/inbox")
def review_inbox(
    limit: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project_ids = _accessible_project_ids(db, current_user.id)
    if not project_ids:
        return {
            "needs_you": [],
            "waiting_on_others": [],
            "recently_closed": [],
        }

    projects = {
        p.id: p for p in db.query(Project).filter(Project.id.in_(project_ids)).all()
    }

    def videos_query():
        return db.query(Video).options(joinedload(Video.uploader)).filter(
            Video.project_id.in_(project_ids)
        )

    # --- Needs you -------------------------------------------------------
    # Three distinct claims on your attention, deduplicated by video so one
    # cut never appears twice with different reasons.
    needs_you: dict[int, dict] = {}

    in_review = (
        videos_query()
        .filter(Video.status == STATUS_IN_REVIEW)
        .order_by(Video.review_due_at.asc().nullslast(), Video.status_changed_at.desc())
        .limit(limit)
        .all()
    )
    sent_by_me = {v.id for v in in_review if v.status_changed_by == current_user.id}

    needs_changes = (
        videos_query()
        .filter(
            Video.status == STATUS_NEEDS_CHANGES,
            Video.uploader_id == current_user.id,
        )
        .order_by(Video.status_changed_at.desc())
        .limit(limit)
        .all()
    )

    assigned = (
        db.query(Comment.video_id, func.count(Comment.id))
        .join(Video, Video.id == Comment.video_id)
        .filter(
            Video.project_id.in_(project_ids),
            Comment.assignee_user_id == current_user.id,
            Comment.parent_id.is_(None),
            Comment.status.notin_(list(TERMINAL_STATUSES)),
        )
        .group_by(Comment.video_id)
        .all()
    )
    assigned_counts = dict(assigned)

    candidate_ids = (
        [v.id for v in in_review]
        + [v.id for v in needs_changes]
        + list(assigned_counts.keys())
    )
    open_comments, open_crs = _open_counts(db, candidate_ids)

    # A cut you sent out yourself is waiting on someone else, not on you.
    for video in in_review:
        if video.id in sent_by_me:
            continue
        needs_you[video.id] = _row(
            video,
            projects.get(video.project_id),
            reason=REASON_REVIEW_REQUESTED,
            open_comments=open_comments.get(video.id, 0),
            open_change_requests=open_crs.get(video.id, 0),
        )

    for video in needs_changes:
        needs_you[video.id] = _row(
            video,
            projects.get(video.project_id),
            reason=REASON_CHANGES_REQUESTED,
            open_comments=open_comments.get(video.id, 0),
            open_change_requests=open_crs.get(video.id, 0),
        )

    if assigned_counts:
        for video in videos_query().filter(Video.id.in_(assigned_counts.keys())).all():
            if video.id in needs_you:
                # Already surfaced for a stronger reason; just carry the count.
                needs_you[video.id]["assigned_to_you"] = assigned_counts[video.id]
                continue
            needs_you[video.id] = _row(
                video,
                projects.get(video.project_id),
                reason=REASON_ASSIGNED_COMMENTS,
                open_comments=open_comments.get(video.id, 0),
                open_change_requests=open_crs.get(video.id, 0),
                extra={"assigned_to_you": assigned_counts[video.id]},
            )

    # --- Waiting on others ----------------------------------------------
    # Cuts you sent, annotated with whether anyone has actually opened them —
    # the difference between "they're thinking" and "they never saw it".
    waiting_videos = [v for v in in_review if v.id in sent_by_me]
    waiting_ids = [v.id for v in waiting_videos]
    opened_at: dict[int, datetime] = {}
    if waiting_ids:
        rows = (
            db.query(ReviewLink.video_id, func.max(ReviewSession.last_viewed_at))
            .join(ReviewSession, ReviewSession.review_link_id == ReviewLink.id)
            .filter(ReviewLink.video_id.in_(waiting_ids))
            .group_by(ReviewLink.video_id)
            .all()
        )
        opened_at = {vid: seen for vid, seen in rows if seen is not None}

    waiting_on_others = [
        _row(
            video,
            projects.get(video.project_id),
            reason=REASON_REVIEW_REQUESTED,
            open_comments=open_comments.get(video.id, 0),
            open_change_requests=open_crs.get(video.id, 0),
            extra={
                "sent_at": video.status_changed_at,
                "last_opened_at": opened_at.get(video.id),
                "opened": video.id in opened_at,
            },
        )
        for video in waiting_videos
    ]

    # --- Recently closed --------------------------------------------------
    closed_rows = (
        db.query(VideoApproval, Video)
        .join(Video, Video.id == VideoApproval.video_id)
        .filter(
            Video.project_id.in_(project_ids),
            VideoApproval.decision == "approved",
            VideoApproval.superseded_at.is_(None),
        )
        .order_by(VideoApproval.created_at.desc())
        .limit(10)
        .all()
    )
    recently_closed = [
        _row(
            video,
            projects.get(video.project_id),
            reason="approved",
            extra={
                "approved_at": approval.created_at,
                "approved_by": (
                    approval.actor.name
                    if approval.actor
                    else (
                        approval.review_session.guest_name
                        if approval.review_session
                        else None
                    )
                ),
            },
        )
        for approval, video in closed_rows
    ]

    def sort_key(row: dict):
        # Overdue first, then soonest due, then most recently moved. Rows with
        # no deadline sort after dated ones rather than jumping the queue.
        due = row.get("review_due_at")
        return (
            0 if due else 1,
            due or datetime.max.replace(tzinfo=timezone.utc),
            -(row.get("open_change_requests") or 0),
        )

    return {
        "needs_you": sorted(needs_you.values(), key=sort_key)[:limit],
        "waiting_on_others": waiting_on_others[:limit],
        "recently_closed": recently_closed,
    }


@router.get("/inbox/summary")
def review_inbox_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Just the count, for the sidebar badge. Polled, so it stays cheap.

    Same claims as the page's "needs you" section — the badge saying 3 while
    the page shows 1 teaches people the badge lies. In particular, a cut *you*
    sent for review is waiting on someone else, so it must not count here.
    """
    project_ids = _accessible_project_ids(db, current_user.id)
    if not project_ids:
        return {"needs_you_count": 0}

    video_ids: set[int] = set()

    # Cuts awaiting review that you did not send out yourself.
    for (vid,) in (
        db.query(Video.id)
        .filter(
            Video.project_id.in_(project_ids),
            Video.status == STATUS_IN_REVIEW,
            or_(
                Video.status_changed_by.is_(None),
                Video.status_changed_by != current_user.id,
            ),
        )
        .all()
    ):
        video_ids.add(vid)

    # Cuts sent back to you.
    for (vid,) in (
        db.query(Video.id)
        .filter(
            Video.project_id.in_(project_ids),
            Video.status == STATUS_NEEDS_CHANGES,
            Video.uploader_id == current_user.id,
        )
        .all()
    ):
        video_ids.add(vid)

    # Videos carrying open comments assigned to you.
    for (vid,) in (
        db.query(Comment.video_id)
        .join(Video, Video.id == Comment.video_id)
        .filter(
            Video.project_id.in_(project_ids),
            Comment.assignee_user_id == current_user.id,
            Comment.parent_id.is_(None),
            Comment.status.notin_(list(TERMINAL_STATUSES)),
        )
        .distinct()
        .all()
    ):
        video_ids.add(vid)

    return {"needs_you_count": len(video_ids)}
