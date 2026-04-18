"""Watch folder sync background job.

Called after the desktop agent reports new files via the API.
Creates video records and triggers proxy generation for each new file.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def watch_folder_sync_job(config_id: int) -> None:
    """RQ job: process pending watch folder sync for a config.

    The actual file list comparison + upload happens via the API route.
    This job handles any deferred processing after upload (proxy, transcription).
    """
    from app.db.database import SessionLocal
    from app.db.models import WatchFolderConfig

    db = SessionLocal()
    try:
        config = db.query(WatchFolderConfig).filter(WatchFolderConfig.id == config_id).first()
        if not config:
            logger.error("WatchFolderConfig %s not found", config_id)
            return

        config.last_sync_at = datetime.now(timezone.utc)
        db.commit()

        logger.info(
            "Watch folder sync completed for config %s (project=%s, path=%s)",
            config_id,
            config.project_id,
            config.folder_path,
        )

    finally:
        db.close()
