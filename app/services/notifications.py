"""One way to raise a notification.

This block used to be copy-pasted three times — `comments.py` (mentions and
owner alerts), `review_links.py` (guest mentions), and `review_links.py` again
(workflow stages) — each ~30 lines, each with slightly different payload keys,
and one of them (the sync `approve` handler) unable to reach the WebSocket at
all because it wasn't `async`. Every future notification type would have
inherited a fourth copy.

Two behaviours live here that no call site had before:

* **Coalescing.** A client leaving twenty comments should produce one
  notification, not twenty. Specs carrying a `group_key` fold into an existing
  unread row inside `GROUP_WINDOW_MINUTES` rather than inserting a new one.
* **An actor.** Rows record who caused them, so the UI can say "Sarah left 7
  comments" without re-fetching the comment to find out.

Emails stay at the call sites: they are type-specific (different templates,
different preference gates) and folding them in here would turn this into a
switch statement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy.orm import Session

from app.db.models import Notification
from app.websocket_manager import notifications_ws_manager

logger = logging.getLogger(__name__)

# How long a notification stays open to absorb siblings. Fifteen minutes is
# long enough to batch a review session's worth of comments and short enough
# that "you have a new comment" still means today.
GROUP_WINDOW_MINUTES = 15

# Notification types (free-form `String` on the model, so adding one needs no
# migration — but does need a push title in app/jobs/push_notifications.py and
# a META entry in the frontend's notification-presentation.tsx).
TYPE_MENTION = "mention"
TYPE_COMMENT = "comment"
TYPE_CLIENT_COMMENT = "client_comment"
TYPE_APPROVAL = "approval"
TYPE_REVIEW_WORKFLOW = "review_workflow"
TYPE_REVIEW_REQUESTED = "review_requested"
TYPE_VIDEO_APPROVED = "video_approved"
TYPE_CHANGES_REQUESTED = "changes_requested"
TYPE_NEW_VERSION = "new_version"

# Decisions must never be hidden inside a batch — one event, one notification.
# Conversation volume is what needs taming, not the answer everyone is waiting on.
NON_GROUPABLE_TYPES = frozenset(
    {
        TYPE_APPROVAL,
        TYPE_REVIEW_REQUESTED,
        TYPE_VIDEO_APPROVED,
        TYPE_CHANGES_REQUESTED,
        TYPE_NEW_VERSION,
    }
)


@dataclass(frozen=True)
class NotificationSpec:
    """One notification to raise, before it becomes a row."""

    user_id: int
    type: str
    project_id: int | None = None
    video_id: int | None = None
    comment_id: int | None = None
    workspace_id: int | None = None
    message: str | None = None
    actor_user_id: int | None = None
    # Siblings sharing this key inside the window fold into one row. Leave None
    # to always insert.
    group_key: str | None = None


def _ws_payload(notification: Notification) -> dict:
    return {
        "event": "notification.new",
        "payload": {
            "id": notification.id,
            "type": notification.type,
            "read": notification.read,
            "project_id": notification.project_id,
            "video_id": notification.video_id,
            "comment_id": notification.comment_id,
            "message": notification.message,
            "created_at": (
                notification.created_at.isoformat() if notification.created_at else None
            ),
        },
    }


def _find_groupable(db: Session, spec: NotificationSpec) -> Notification | None:
    if not spec.group_key or spec.type in NON_GROUPABLE_TYPES:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=GROUP_WINDOW_MINUTES)
    return (
        db.query(Notification)
        .filter(
            Notification.user_id == spec.user_id,
            Notification.type == spec.type,
            Notification.group_key == spec.group_key,
            Notification.read.is_(False),
            Notification.created_at >= cutoff,
        )
        .order_by(Notification.created_at.desc())
        .first()
    )


def build_notifications(db: Session, specs: Iterable[NotificationSpec]) -> list[Notification]:
    """Create or fold rows for `specs` and flush. Does not commit, and does not
    deliver — use `emit_notifications` unless you need to control the
    transaction yourself.
    """
    rows: list[Notification] = []
    for spec in specs:
        if not spec.user_id:
            continue

        existing = _find_groupable(db, spec)
        if existing is not None:
            existing.group_count = (existing.group_count or 1) + 1
            existing.created_at = datetime.now(timezone.utc)
            if spec.message:
                existing.message = spec.message
            if spec.comment_id:
                existing.comment_id = spec.comment_id
            rows.append(existing)
            continue

        notification = Notification(
            user_id=spec.user_id,
            type=spec.type,
            project_id=spec.project_id,
            video_id=spec.video_id,
            comment_id=spec.comment_id,
            workspace_id=spec.workspace_id,
            message=spec.message,
            actor_user_id=spec.actor_user_id,
            group_key=spec.group_key,
            group_count=1,
            read=False,
        )
        db.add(notification)
        rows.append(notification)

    if rows:
        db.flush()
    return rows


async def deliver_notifications(notifications: Iterable[Notification]) -> None:
    """Fan out to native push and any live WebSocket. Best-effort: a failed
    delivery must never roll back the row that was already committed."""
    from app.jobs.queue import enqueue_push_notification_job

    for notification in notifications:
        try:
            enqueue_push_notification_job(notification.user_id, notification.id)
        except Exception:
            logger.exception(
                "Push enqueue failed for notification %s", notification.id
            )
        try:
            await notifications_ws_manager.send_to_user(
                notification.user_id, _ws_payload(notification)
            )
        except Exception:
            logger.exception(
                "WebSocket delivery failed for notification %s", notification.id
            )


def emit_notifications_sync(
    db: Session, specs: Iterable[NotificationSpec]
) -> list[Notification]:
    """The emit path for sync handlers, which run in FastAPI's threadpool and
    cannot await.

    Delivers rows + native push. Live WebSocket delivery is skipped — the
    bell's 20s polling fallback picks these up, which is an acceptable delay
    for "a new version landed". The alternative (making the multipart upload
    handler async) would park its blocking file I/O on the event loop.
    """
    rows = build_notifications(db, specs)
    if not rows:
        return []
    db.commit()
    for row in rows:
        db.refresh(row)

    from app.jobs.queue import enqueue_push_notification_job

    for row in rows:
        try:
            enqueue_push_notification_job(row.user_id, row.id)
        except Exception:
            logger.exception("Push enqueue failed for notification %s", row.id)
    return rows


async def emit_notifications(
    db: Session, specs: Iterable[NotificationSpec]
) -> list[Notification]:
    """Create/fold the rows, commit, then deliver.

    Commits because delivery must not reference rows that could still roll
    back — a push notification pointing at a nonexistent id is worse than no
    push at all.
    """
    rows = build_notifications(db, specs)
    if not rows:
        return []
    db.commit()
    for row in rows:
        db.refresh(row)
    await deliver_notifications(rows)
    return rows
