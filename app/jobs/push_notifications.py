from __future__ import annotations

import json
import logging
import urllib.request

from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.db.models import DevicePushToken, Notification

logger = logging.getLogger(__name__)


# Every type raised by app/services/notifications.py needs an entry here, or
# it silently degrades to "New notification" on the lock screen.
_TITLES: dict[str, str] = {
    "mention": "You were mentioned",
    "comment": "New comment",
    "client_comment": "Client feedback",
    "approval": "Review approved",
    "video_approved": "Approved",
    "changes_requested": "Changes requested",
    "review_requested": "Review requested",
    "new_version": "New version",
    "review_workflow": "Review workflow update",
    "workspace_invite": "Workspace invitation",
    "project_invite": "Project invitation",
}

_BODIES: dict[str, str] = {
    "mention": "A teammate mentioned you in a comment.",
    "comment": "Someone commented on your video.",
    "client_comment": "Your client left feedback on a video.",
    "approval": "A client approved your review link.",
    "video_approved": "A cut you worked on has been signed off.",
    "changes_requested": "A reviewer sent a cut back with changes.",
    "review_requested": "Someone asked you to review a cut.",
    "new_version": "A new version is ready to review.",
    "review_workflow": "A review workflow stage has changed.",
}


def _title_for_notification(notification: Notification) -> str:
    notification_type = (notification.type or "").strip().lower()
    return _TITLES.get(notification_type, "New notification")


def _body_for_notification(notification: Notification) -> str:
    notification_type = (notification.type or "").strip().lower()
    # The emitter writes a specific, already-humanised message ("Sarah Client
    # requested a change on Summer Reel"); prefer it over the generic copy.
    if notification.message:
        return notification.message
    return _BODIES.get(notification_type, "Open the app to view details.")


def _send_expo_push(token: str, payload: dict) -> None:
    req = urllib.request.Request(
        "https://exp.host/--/api/v2/push/send",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        response.read()


def send_push_notification_job(user_id: int, notification_id: int) -> bool:
    db: Session = SessionLocal()
    try:
        notification = (
            db.query(Notification)
            .filter(
                Notification.id == notification_id,
                Notification.user_id == user_id,
            )
            .first()
        )
        if not notification:
            return False
        tokens = (
            db.query(DevicePushToken)
            .filter(
                DevicePushToken.user_id == user_id,
                DevicePushToken.enabled == True,  # noqa: E712
            )
            .all()
        )
        if not tokens:
            return True

        deep_link = f"editube://notifications/{notification.id}"
        for token in tokens:
            payload = {
                "to": token.token,
                "title": _title_for_notification(notification),
                "body": _body_for_notification(notification),
                "data": {
                    "notification_id": notification.id,
                    "type": notification.type,
                    "project_id": notification.project_id,
                    "video_id": notification.video_id,
                    "comment_id": notification.comment_id,
                    "workspace_id": notification.workspace_id,
                    "workspace_invite_id": notification.workspace_invite_id,
                    "invite_token": notification.invite_token,
                    "message": notification.message,
                    "deep_link": deep_link,
                },
                "sound": "default",
            }
            try:
                _send_expo_push(token.token, payload)
            except Exception:
                logger.exception(
                    "Push send failed for token_id=%s notification_id=%s",
                    token.id,
                    notification.id,
                )
        return True
    finally:
        db.close()
