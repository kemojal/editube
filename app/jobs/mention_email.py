from __future__ import annotations

import logging

from app.db.database import SessionLocal
from app.db.models import User, UserSettings
from app.utils.email import send_comment_mention_email, send_new_comment_email

logger = logging.getLogger(__name__)


def send_mention_email_job(
    recipient_user_id: int,
    actor_name: str,
    project_name: str | None,
    video_name: str | None,
    comment_text: str,
    comment_url: str,
) -> None:
    db = SessionLocal()
    try:
        recipient = db.query(User).filter(User.id == recipient_user_id).first()
        if not recipient or not recipient.email:
            return

        settings = (
            db.query(UserSettings)
            .filter(UserSettings.user_id == recipient_user_id)
            .first()
        )
        if settings is not None and not settings.email_mentions:
            logger.info("Skipping mention email for user %s: disabled in settings", recipient_user_id)
            return

        sent = send_comment_mention_email(
            to_email=recipient.email,
            recipient_name=recipient.name,
            actor_name=actor_name,
            project_name=project_name,
            video_name=video_name,
            comment_text=comment_text,
            comment_url=comment_url,
        )
        if not sent:
            logger.warning("Mention email send returned false for user %s", recipient_user_id)
    except Exception:
        logger.exception("Mention email job failed for user %s", recipient_user_id)
    finally:
        db.close()


def send_comment_notification_email_job(
    recipient_user_id: int,
    actor_name: str,
    project_name: str | None,
    video_name: str | None,
    comment_text: str,
    comment_url: str,
) -> None:
    """Email a video/project owner that someone commented, gated by email_comments."""
    db = SessionLocal()
    try:
        recipient = db.query(User).filter(User.id == recipient_user_id).first()
        if not recipient or not recipient.email:
            return

        settings = (
            db.query(UserSettings).filter(UserSettings.user_id == recipient_user_id).first()
        )
        if settings is not None and not settings.email_comments:
            logger.info("Skipping comment email for user %s: disabled in settings", recipient_user_id)
            return

        sent = send_new_comment_email(
            to_email=recipient.email,
            recipient_name=recipient.name,
            actor_name=actor_name,
            project_name=project_name,
            video_name=video_name,
            comment_text=comment_text,
            comment_url=comment_url,
        )
        if not sent:
            logger.warning("Comment email send returned false for user %s", recipient_user_id)
    except Exception:
        logger.exception("Comment email job failed for user %s", recipient_user_id)
    finally:
        db.close()
