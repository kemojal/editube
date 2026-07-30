"""Central lookups for a user's notification/email preferences.

Every place that decides whether to send a notification email should go through
these helpers so the Settings → Notifications toggles are honoured consistently.
Missing settings fall back to the same defaults as ``DEFAULT_USER_SETTINGS``.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import UserSettings


def _settings(db: Session, user_id: int) -> UserSettings | None:
    return db.query(UserSettings).filter(UserSettings.user_id == user_id).first()


def wants_comment_emails(db: Session, user_id: int) -> bool:
    """Email me when someone comments on my work (default on)."""
    s = _settings(db, user_id)
    return True if s is None else bool(s.email_comments)


def wants_mention_emails(db: Session, user_id: int) -> bool:
    """Email me when someone @mentions me (default on)."""
    s = _settings(db, user_id)
    return True if s is None else bool(s.email_mentions)


def wants_product_updates(db: Session, user_id: int) -> bool:
    """Send me product-update / marketing email (default off).

    No broadcast campaign exists yet; any future one must gate on this.
    """
    s = _settings(db, user_id)
    return False if s is None else bool(s.product_updates)


def allows_project_invites(db: Session, user_id: int) -> bool:
    """Allow others to invite me to projects/workspaces via email (default on)."""
    s = _settings(db, user_id)
    return True if s is None else bool(s.allow_project_invites)
