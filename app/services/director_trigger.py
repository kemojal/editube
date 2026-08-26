"""Starting the director on its own, once the cut is ready.

The wizard's "AI creative director" toggle is a **one-shot consent**, exactly
like the auto-edit toggle it sits under: the user asked for this video to be
directed, once, at the moment the cut became available. It is not a standing
instruction to re-direct the piece every time transcription is re-run, and the
bug this guard exists to prevent is precisely that — a second run generating a
second budget of images over a timeline someone has since been editing.

So the consent is spent the moment it is acted on: `enabled` is cleared on the
preferences row before the job is enqueued, and a prefs row that has already
been spent can never start another run. This mirrors `auto-edit-gate.ts`, which
learned the same lesson for cuts.

Never raises. A director run that fails must not fail the transcription job it
hangs off — the transcript and the cut are the valuable part and are already
done by the time this is called.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

PREFS_TYPE = "director_prefs"


def default_prefs() -> dict[str, Any]:
    return {"enabled": False, "tier": "standard", "brief": "", "allow_video": True}


def run_post_cut_director(db: Any, video_id: int) -> int | None:
    """Start a director run if this video was signed up for one.

    Called after the post-transcription auto-edit has seeded the cut, so the
    director reads the video as it will actually be watched rather than the
    uncut take.

    Returns the new plan id, or None when nothing was started.
    """
    from app.db.models import AiResult, DirectorPlan, Video, VideoTranscription
    from app.services import claude_client

    try:
        prefs_row = (
            db.query(AiResult)
            .filter(AiResult.video_id == video_id, AiResult.result_type == PREFS_TYPE)
            .first()
        )
        if not prefs_row or not isinstance(prefs_row.result_data, dict):
            return None
        prefs = dict(prefs_row.result_data)
        if not prefs.get("enabled"):
            return None

        if not claude_client.available():
            # Told on the row rather than silently skipped: the user turned this
            # on and is entitled to know why nothing happened.
            prefs["enabled"] = False
            prefs["skippedReason"] = "The creative director is not configured on this server."
            prefs_row.result_data = prefs
            db.commit()
            logger.warning("Director requested for video %s but no API key is configured", video_id)
            return None

        transcription = (
            db.query(VideoTranscription)
            .filter(VideoTranscription.video_id == video_id)
            .first()
        )
        if not transcription or not transcription.segments:
            return None

        existing = (
            db.query(DirectorPlan)
            .filter(
                DirectorPlan.video_id == video_id,
                DirectorPlan.status.in_(
                    ("queued", "planning", "generating", "compiling", "ready", "applied")
                ),
            )
            .first()
        )
        if existing is not None:
            # Already directed, or being directed. A second run would generate a
            # second budget of images over work someone may already have edited.
            logger.info("Director already run for video %s (plan %s)", video_id, existing.id)
            return None

        video = db.query(Video).filter(Video.id == video_id).first()
        if video is None:
            return None

        # Spend the consent *before* enqueuing. If the job crashes and the
        # transcription is retried, the user gets one run and one bill, not one
        # per retry.
        prefs["enabled"] = False
        prefs["spent"] = True
        prefs_row.result_data = prefs

        plan_row = DirectorPlan(
            video_id=video_id,
            project_id=video.project_id,
            user_id=video.uploader_id,
            status="queued",
            stage="Queued",
            tier=str(prefs.get("tier") or "standard"),
            brief=(str(prefs.get("brief") or "").strip() or None),
            allow_video=bool(prefs.get("allow_video", True)),
        )
        db.add(plan_row)
        db.commit()
        db.refresh(plan_row)

        from app.jobs.queue import enqueue_director_job

        if enqueue_director_job(plan_row.id) is None:
            plan_row.status = "failed"
            plan_row.stage = None
            plan_row.error_message = "No worker queue configured (REDIS_URL is unset)."
            db.commit()
            return plan_row.id

        logger.info("Director run %s started for video %s", plan_row.id, video_id)
        return plan_row.id

    except Exception:
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        # The transcript and the cut are already done and are the valuable part.
        # A failed director must not take them down with it.
        logger.exception("Post-cut director trigger failed for video %s", video_id)
        return None
