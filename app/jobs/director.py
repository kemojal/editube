"""RQ job: read the cut, direct it, and record the plan.

Stages advance the row rather than being held in memory, so a run that dies
half-way can be resumed instead of restarted — regenerating a dozen images
because the compile step crashed would be slow and expensive, and the plan
itself is the cheap part to keep.

This job stops at `ready`. Generating the assets and compiling them onto the
timeline are separate stages (M3/M4); until they exist the plan is something the
user reviews rather than something that lands on its own.
"""

from __future__ import annotations

import logging
from typing import Any, NoReturn

from app.db.database import SessionLocal
from app.db.models import AiResult, DirectorPlan, Video, VideoTranscription

logger = logging.getLogger(__name__)


def _draft_keep_ranges(db: Any, video_id: int) -> list[dict[str, Any]]:
    """The cut as it stands, from the rough-cut draft.

    Absent or empty means nothing was cut, which `CutMap` treats as the whole
    take — the same reading the editor's own `isWholeTakeTimeline` uses.
    """
    row = (
        db.query(AiResult)
        .filter(AiResult.video_id == video_id, AiResult.result_type == "rough_cut_draft")
        .first()
    )
    data = row.result_data if row and isinstance(row.result_data, dict) else {}
    ranges = data.get("keepRanges")
    return ranges if isinstance(ranges, list) else []


def _project_aspect(draft: dict[str, Any]) -> str:
    """What the project renders at.

    A 16:9 still in a 9:16 export letterboxes or crops badly, so the director is
    told the target rather than left to assume one (§12).
    """
    layout = draft.get("layoutStyle") if isinstance(draft.get("layoutStyle"), dict) else {}
    aspect = str(layout.get("aspect") or "").strip()
    return aspect if aspect in {"16:9", "9:16", "1:1"} else "16:9"


def _fail(db: Any, row: DirectorPlan, message: str) -> NoReturn:
    row.status = "failed"
    row.stage = None
    row.error_message = message[:2000]
    db.commit()
    raise RuntimeError(message)


def director_job(plan_id: int) -> dict[str, Any]:
    """Entry point registered with RQ."""
    from app.services import director_service
    from app.services.director_context import build_context

    db = SessionLocal()
    try:
        row = db.query(DirectorPlan).filter(DirectorPlan.id == plan_id).first()
        if row is None:
            raise RuntimeError(f"Director plan {plan_id} was removed before processing")
        if row.cancel_requested:
            row.status = "cancelled"
            row.stage = None
            db.commit()
            return {"status": "cancelled", "id": plan_id}

        video = db.query(Video).filter(Video.id == row.video_id).first()
        if video is None:
            return _fail(db, row, "The video was removed before the director could run.")

        transcription = (
            db.query(VideoTranscription)
            .filter(VideoTranscription.video_id == row.video_id)
            .first()
        )
        segments = transcription.segments if transcription and transcription.segments else []
        if not segments:
            # Every shot anchors to something that was said. Told plainly,
            # because "failed" with no reason sends people to the logs.
            return _fail(
                db, row, "This video has no transcript yet, so there is nothing to direct from."
            )

        draft_row = (
            db.query(AiResult)
            .filter(AiResult.video_id == row.video_id, AiResult.result_type == "rough_cut_draft")
            .first()
        )
        draft = draft_row.result_data if draft_row and isinstance(draft_row.result_data, dict) else {}

        row.status = "planning"
        row.stage = "Reading the cut"
        row.progress = 5
        db.commit()

        context = build_context(
            segments=segments,
            keep_ranges=_draft_keep_ranges(db, row.video_id),
            source_duration=float(video.duration or 0) or _draft_duration(draft),
            aspect=_project_aspect(draft),
        )
        if not context.has_speech:
            return _fail(
                db, row, "The transcript has no usable speech, so there is nothing to direct from."
            )

        row.stage = "Writing the treatment"
        row.progress = 20
        db.commit()

        try:
            run = director_service.generate_plan(
                context,
                director_service.DirectorOptions(
                    tier=row.tier, brief=row.brief or "", allow_video=row.allow_video
                ),
            )
        except director_service.DirectorUnavailable as exc:
            return _fail(db, row, str(exc))
        except director_service.PlanRejected as exc:
            return _fail(db, row, f"The plan could not be used: {exc}")

        # A cancel that arrived while the model was thinking still counts. The
        # plan is kept — it was paid for — but nothing downstream acts on it.
        db.refresh(row)
        if row.cancel_requested:
            row.status = "cancelled"
            row.stage = None
            row.plan = run.to_dict()
            db.commit()
            return {"status": "cancelled", "id": plan_id}

        row.plan = run.to_dict()
        row.warnings = run.plan.warnings
        row.usage = run.usage
        row.model = run.model
        row.error_message = None
        db.commit()

        # Fan the shots out and hand off. This job does not wait for them: a
        # Veo shot takes minutes, and holding a worker slot open for half an
        # hour to watch a progress field would block every other job on the
        # queue. The API's poll advances the run once the assets settle
        # (`director_assets.reconcile`), the same way `_reconcile_dead_effect`
        # handles effects.
        from app.services import director_assets

        try:
            requested = director_assets.request_assets(db, row, run.to_dict())
        except Exception as exc:  # noqa: BLE001
            logger.exception("Director plan %s could not request assets", plan_id)
            return _fail(db, row, f"The shots could not be queued: {exc}")

        if requested.any_work:
            row.status = "generating"
            row.stage = f"Sourcing {requested.created} shots (0 ready)"
            row.progress = 25
        else:
            # Every shot reuses existing footage, or the plan chose none at all.
            row.status = "ready"
            row.stage = f"Planned {len(run.plan.directives)} shot(s)"
            row.progress = 100
        db.commit()

        logger.info(
            "Director plan %s planned %s shot(s), %s to generate, %s warning(s)",
            plan_id,
            len(run.plan.directives),
            requested.created,
            len(run.plan.warnings),
        )
        return {
            "status": row.status,
            "id": plan_id,
            "directives": len(run.plan.directives),
            "generating": requested.created,
            "warnings": len(run.plan.warnings),
        }

    except Exception as exc:  # noqa: BLE001 - the failure has to reach the row
        logger.exception("Director plan %s failed", plan_id)
        try:
            db.rollback()
            row = db.query(DirectorPlan).filter(DirectorPlan.id == plan_id).first()
            if row is not None and row.status != "failed":
                row.status = "failed"
                row.stage = None
                row.error_message = str(exc)[:2000]
                db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("Could not record the director failure for %s", plan_id)
        raise
    finally:
        db.close()


def _draft_duration(draft: dict[str, Any]) -> float:
    """Fallback source length when the video row has none.

    The draft records the duration it was cut against, which is more trustworthy
    than a missing `videos.duration` — the same reasoning the editor's own
    `draftRangeDuration` uses.
    """
    try:
        return max(0.0, float(draft.get("sourceDuration") or 0.0))
    except (TypeError, ValueError):
        return 0.0
