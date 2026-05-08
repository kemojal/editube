import json
import logging
from typing import Optional
from sqlalchemy.orm import Session
from app.db.models import ActivityFeed

logger = logging.getLogger(__name__)


def log_activity(
    db: Session,
    *,
    user_id: int,
    project_id: int,
    action: str,
    meta: Optional[dict] = None,
) -> None:
    try:
        entry = ActivityFeed(
            user_id=user_id,
            project_id=project_id,
            action=action,
            meta_info=json.dumps(meta or {}),
        )
        db.add(entry)
    except Exception:
        logger.exception("Failed to log activity: action=%s project=%s", action, project_id)
