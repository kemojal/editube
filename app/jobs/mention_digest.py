"""Batch @mention digest email (daily/weekly preference on user_settings)."""

from __future__ import annotations

import logging
import os

from app.db.database import SessionLocal
from app.db.models import Notification, User, UserSettings, Video
from app.utils.email import send_transactional_email

logger = logging.getLogger(__name__)


def send_mention_digest_job(user_id: int) -> None:
    """Send one digest email summarizing recent unread mention notifications."""
    db = SessionLocal()
    try:
        settings = (
            db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
        )
        if not settings or settings.email_mention_digest not in ("daily", "weekly"):
            return
        if not settings.email_mentions:
            return
        recipient = db.query(User).filter(User.id == user_id).first()
        if not recipient or not recipient.email:
            return

        notes = (
            db.query(Notification)
            .filter(
                Notification.user_id == user_id,
                Notification.type == "mention",
                Notification.read == False,  # noqa: E712
            )
            .order_by(Notification.created_at.desc())
            .limit(40)
            .all()
        )
        if not notes:
            return

        base = os.getenv("FRONTEND_BASE_URL", "http://localhost:3000").rstrip("/")
        lines: list[str] = []
        for n in notes:
            vname = ""
            if n.video_id:
                v = db.query(Video).filter(Video.id == n.video_id).first()
                vname = f" — {v.name}" if v and v.name else ""
            if n.video_id:
                q = "tab=comments"
                if n.comment_id is not None:
                    q += f"&commentId={n.comment_id}"
                url = f"{base}/player/{n.video_id}?{q}"
            elif n.project_id is not None:
                url = f"{base}/projects/{n.project_id}"
            else:
                url = f"{base}/notifications"
            lines.append(f"-{vname}\n  {url}")

        body = (
            f"Hi {recipient.name or 'there'},\n\n"
            f"You have {len(notes)} unread @mention(s) on Editube:\n\n"
            + "\n".join(lines)
            + "\n\nOpen notifications in the app to mark them read.\n"
        )
        sent = send_transactional_email(
            to=recipient.email,
            subject=f"Editube: {len(notes)} @mentions digest",
            body_text=body,
        )
        if not sent:
            logger.warning("Mention digest email not sent for user %s", user_id)
    except Exception:
        logger.exception("Mention digest job failed for user %s", user_id)
    finally:
        db.close()


def run_digest_for_all_users() -> None:
    """Intended for RQ/cron: fan out one digest job per user with digest enabled."""
    db = SessionLocal()
    try:
        rows = (
            db.query(UserSettings.user_id)
            .filter(UserSettings.email_mention_digest.in_(("daily", "weekly")))
            .all()
        )
        for (uid,) in rows:
            send_mention_digest_job(int(uid))
    finally:
        db.close()
