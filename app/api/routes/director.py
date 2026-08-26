"""AI creative director endpoints.

The editor has no WebSocket, so this is poll-driven like the rest of the
rough-cut surface: start a run, poll it while a sentence describes what it is
doing, then open the editor to a directed cut.

One run per video at a time, enforced here rather than in the worker. Two
concurrent runs would each generate a full budget of images, and the second
would overwrite the first's plan after paying for it.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import DirectorPlan, GeneratedMedia, Project, User, Video
from app.services import claude_client, director_manifest
from app.services.director_service import BUDGET_TIERS
from app.services.project_access import can_access_project
from app.utils.security import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/videos", tags=["ai-director"])

#: Statuses where a run still owns the video and a second one must not start.
ACTIVE_STATUSES = ("queued", "planning", "generating", "compiling")
#: Statuses a run can be cancelled from. `degraded` is included: it is a
#: finished run whose assets partly failed, and a user may well want to stop
#: there rather than apply a thinner edit than they asked for.
CANCELLABLE_STATUSES = ACTIVE_STATUSES + ("ready", "degraded")


def _video_or_403(video_id: int, db: Session, user: User) -> Video:
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    project = db.query(Project).filter(Project.id == video.project_id).first()
    if not project or not can_access_project(db, user.id, project):
        raise HTTPException(status_code=403, detail="Not authorized to access this video")
    return video


def _latest(db: Session, video_id: int) -> DirectorPlan | None:
    return (
        db.query(DirectorPlan)
        .filter(DirectorPlan.video_id == video_id)
        .order_by(DirectorPlan.created_at.desc())
        .first()
    )


def _serialize(db: Session, row: DirectorPlan) -> dict[str, Any]:
    """The whole run, including per-asset progress.

    Asset progress is joined in rather than duplicated onto the plan row: the
    generation worker already maintains it on `generated_media`, and a second
    copy would be a second thing to keep in sync.
    """
    assets = (
        db.query(GeneratedMedia)
        .filter(GeneratedMedia.director_plan_id == row.id)
        .order_by(GeneratedMedia.id.asc())
        .all()
    )
    ready = sum(1 for asset in assets if asset.status == "ready")
    return {
        "id": row.id,
        "video_id": row.video_id,
        "status": row.status,
        "stage": row.stage,
        "progress": row.progress,
        "tier": row.tier,
        "brief": row.brief,
        "allow_video": row.allow_video,
        "plan": row.plan,
        "warnings": row.warnings or [],
        "usage": row.usage,
        "model": row.model,
        "error_message": row.error_message,
        "applied_at": row.applied_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "assets": {
            "total": len(assets),
            "ready": ready,
            "failed": sum(1 for asset in assets if asset.status == "failed"),
            "items": [
                {
                    "id": asset.id,
                    "directive_id": asset.director_directive_id,
                    "kind": asset.kind,
                    "status": asset.status,
                    "progress": asset.progress,
                    "url": asset.url,
                    "error_message": asset.error_message,
                }
                for asset in assets
            ],
        },
    }


class StartDirectorBody(BaseModel):
    tier: str = Field(default="standard")
    brief: str = Field(default="", max_length=2000)
    allow_video: bool = True


class DirectorPrefsBody(StartDirectorBody):
    """The wizard's toggle, captured before there is anything to direct.

    `enabled` is a **one-shot consent**, not a setting: it is spent the moment
    the post-transcription hook acts on it (`director_trigger`). Storing it as a
    standing preference would re-direct the piece on every re-transcription, a
    second budget of images at a time.
    """

    enabled: bool = False


@router.put("/{video_id}/ai/director-prefs")
def save_director_prefs(
    video_id: int,
    body: DirectorPrefsBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _video_or_403(video_id, db, current_user)
    if body.tier not in BUDGET_TIERS:
        raise HTTPException(status_code=422, detail=f"Unknown tier {body.tier!r}")

    from app.db.models import AiResult
    from app.services.director_trigger import PREFS_TYPE

    row = (
        db.query(AiResult)
        .filter(AiResult.video_id == video_id, AiResult.result_type == PREFS_TYPE)
        .first()
    )
    if row is None:
        row = AiResult(video_id=video_id, result_type=PREFS_TYPE)
        db.add(row)
    row.status = "completed"
    row.error_message = None
    row.result_data = body.model_dump(mode="json")
    db.commit()
    db.refresh(row)
    return {"video_id": video_id, "result_data": row.result_data}


@router.get("/{video_id}/ai/director-prefs")
def get_director_prefs(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _video_or_403(video_id, db, current_user)

    from app.db.models import AiResult
    from app.services.director_trigger import PREFS_TYPE, default_prefs

    row = (
        db.query(AiResult)
        .filter(AiResult.video_id == video_id, AiResult.result_type == PREFS_TYPE)
        .first()
    )
    return {"video_id": video_id, "result_data": row.result_data if row else default_prefs()}


@router.get("/{video_id}/ai/director/capabilities")
def director_capabilities(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """What the director can do here, and whether it can run at all.

    The wizard asks before offering the toggle: a deployment with no key should
    not show a feature that will refuse on submit.
    """
    _video_or_403(video_id, db, current_user)
    return {
        "available": claude_client.available(),
        "tiers": {name: {"images": images, "videos": videos} for name, (images, videos) in BUDGET_TIERS.items()},
        "manifest": director_manifest.as_dict(),
    }


@router.post("/{video_id}/ai/director")
def start_director(
    video_id: int,
    body: StartDirectorBody | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    video = _video_or_403(video_id, db, current_user)
    options = body or StartDirectorBody()

    if not claude_client.available():
        raise HTTPException(
            status_code=503,
            detail="The creative director is not configured on this server.",
        )
    if options.tier not in BUDGET_TIERS:
        raise HTTPException(status_code=422, detail=f"Unknown tier {options.tier!r}")

    existing = _latest(db, video_id)
    if existing is not None and existing.status in ACTIVE_STATUSES:
        # Returning the run in flight rather than 409ing: a double-submit from an
        # impatient click should land on the thing already happening.
        return _serialize(db, existing)

    row = DirectorPlan(
        video_id=video_id,
        project_id=video.project_id,
        user_id=current_user.id,
        status="queued",
        stage="Queued",
        tier=options.tier,
        brief=options.brief.strip() or None,
        allow_video=options.allow_video,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    from app.jobs.queue import enqueue_director_job

    if enqueue_director_job(row.id) is None:
        row.status = "failed"
        row.stage = None
        row.error_message = "No worker queue configured (REDIS_URL is unset)."
        db.commit()
        db.refresh(row)
    return _serialize(db, row)


@router.get("/{video_id}/ai/director")
def get_director(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _video_or_403(video_id, db, current_user)
    row = _latest(db, video_id)
    if row is None:
        return {"status": "none", "video_id": video_id, "available": claude_client.available()}

    # The planning job hands off once the shots are queued rather than holding a
    # worker slot open for the minutes a Veo shot takes, so this poll is what
    # advances the run when its assets settle. Without it a finished run would
    # sit at "generating" forever with every asset already done.
    from app.services import director_assets

    if director_assets.reconcile(db, row):
        db.refresh(row)
    return _serialize(db, row)


@router.post("/{video_id}/ai/director/apply")
def apply_director(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Put the planned shots on the timeline.

    Explicit rather than automatic while the review UI is being built, and
    additive either way — compiling never removes existing clips, so this cannot
    destroy editing done in the meantime.
    """
    _video_or_403(video_id, db, current_user)
    row = _latest(db, video_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No director run to apply")

    from app.services import director_apply

    try:
        director_apply.apply_plan(db, row)
    except director_apply.NotApplicable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(row)
    return _serialize(db, row)


@router.delete("/{video_id}/ai/director/apply")
def revert_director(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Take the run's shots back off the timeline, leaving everything else."""
    _video_or_403(video_id, db, current_user)
    row = _latest(db, video_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No director run to revert")

    from app.services import director_apply

    if not director_apply.revert(db, row):
        raise HTTPException(status_code=409, detail="This run is not currently applied")
    db.refresh(row)
    return _serialize(db, row)


@router.post("/{video_id}/ai/director/cancel")
def cancel_director(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Stop a run, and stop everything it has in flight.

    The flag propagates to every asset the run started: the generation worker
    checks it between polls, which is the only way to abandon a Veo job that
    would otherwise hold a worker for minutes.
    """
    _video_or_403(video_id, db, current_user)
    row = _latest(db, video_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No director run to cancel")
    if row.status not in CANCELLABLE_STATUSES:
        return _serialize(db, row)

    row.cancel_requested = True
    db.query(GeneratedMedia).filter(
        GeneratedMedia.director_plan_id == row.id,
        GeneratedMedia.status.in_(("pending", "running")),
    ).update({"cancel_requested": True}, synchronize_session=False)
    db.commit()
    db.refresh(row)
    return _serialize(db, row)
