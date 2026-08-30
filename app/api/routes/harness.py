"""Editing-harness HTTP surface.

Mounted under the `/videos` convention like the Director, with two deliberate
differences learned from its defects (plan §2): runs are addressed **by id**,
never by "latest", and every mutating route requires **write**-level project
permission (`assert_write_project_content`) — the read-level check the draft
routes historically used let guests PUT a timeline.

Transport is polling, matching the editor. `GET` a run also reconciles it
(advancing or failing a stale one), and the response always carries the run's
full state so the client needs no second request.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import HarnessAutoApplyGrant, HarnessOperation, HarnessRun, Project, User, Video
from app.jobs.queue import enqueue_harness_apply_job
from app.services import draft_store
from app.services.harness import capabilities as caps
from app.services.harness.compiler import RECIPES, list_recipes
from app.services.harness import executor
from app.services.harness.executor import HarnessError
from app.utils.security import get_current_user
from app.services.project_access import assert_write_project_content, can_access_project

router = APIRouter(prefix="/videos", tags=["Editing Harness"])
runs_router = APIRouter(prefix="/editing/runs", tags=["Editing Harness"])


def _video_and_project(
    video_id: int, db: Session, current_user: User, *, write: bool
) -> tuple[Video, Project]:
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    project = db.query(Project).filter(Project.id == video.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if write:
        assert_write_project_content(db, current_user, project)
    elif not can_access_project(db, current_user.id, project):
        raise HTTPException(status_code=403, detail="Not authorized to access this video")
    return video, project


def _run_or_404(run_id: int, db: Session, current_user: User, *, write: bool) -> HarnessRun:
    run = db.query(HarnessRun).filter(HarnessRun.id == run_id).first()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    _video_and_project(run.video_id, db, current_user, write=write)
    return run


def _http(exc: HarnessError) -> HTTPException:
    status = {
        "not_applicable": 409,
        "not_approvable": 409,
        "not_editable": 409,
        "not_cancellable": 409,
        "not_reverted": 409,
        "stale_plan": 409,
        "draft_moved": 409,
        "unknown_operation": 404,
    }.get(exc.code, 400)
    return HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)})


def _serialize_operation(row: HarnessOperation) -> dict[str, Any]:
    return {
        "operation_key": row.operation_key,
        "type": row.type,
        "sequence": row.sequence,
        "depends_on": row.depends_on or [],
        "state": row.state,
        "risk": row.risk,
        "params": row.params,
        "staged_asset": row.staged_asset,
        "error_code": row.error_code,
        "error_detail": row.error_detail,
    }


def _serialize_run(db: Session, run: HarnessRun) -> dict[str, Any]:
    operations = (
        db.query(HarnessOperation)
        .filter(HarnessOperation.run_id == run.id)
        .order_by(HarnessOperation.sequence.asc())
        .all()
    )
    return {
        "id": run.id,
        "video_id": run.video_id,
        "project_id": run.project_id,
        "state": run.state,
        "stage": run.stage,
        "intent": run.intent,
        "recipe_id": run.recipe_id,
        "recipe_version": run.recipe_version,
        "auto_applied": bool(run.auto_applied),
        "params": run.params,
        "plan": run.plan,
        "plan_checksum": run.plan_checksum,
        "diff": run.diff,
        "estimates": run.estimates,
        "warnings": run.warnings or [],
        "base_draft_revision": run.base_draft_revision,
        "applied_draft_revision": run.applied_draft_revision,
        "applied_manifest": run.applied_manifest,
        "verification_report": run.verification_report,
        "error_code": run.error_code,
        "error_detail": run.error_detail,
        "operations": [_serialize_operation(op) for op in operations],
        "created_at": run.created_at,
        "applied_at": run.applied_at,
        "reverted_at": run.reverted_at,
    }


@router.get("/{video_id}/editing/capabilities")
def get_capabilities(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _video_and_project(video_id, db, current_user, write=False)
    snapshot = caps.snapshot()
    return {"capabilities": snapshot, "recipes": list_recipes(snapshot)}


class SelectionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: float = Field(ge=0)
    end: float = Field(gt=0)


class CreateRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipe_id: str = Field(min_length=1, max_length=64)
    #: Explicit parameters (the quick-action path). Empty + `intent` set means
    #: "plan it from the description" via the planner chain.
    params: dict[str, Any] = Field(default_factory=dict)
    intent: str | None = Field(default=None, max_length=2000)
    #: The user's current selection, source seconds — the planner prefers it.
    selection: SelectionBody | None = None
    #: A one-shot auto-apply consent (plan Phase 5). When the compiled plan
    #: qualifies (reversible steps only), the run approves and applies with no
    #: further click and the grant is spent; otherwise the run stays planned,
    #: the grant stays unspent, and a warning says why.
    auto_apply_grant_id: int | None = Field(default=None, gt=0)


@router.post("/{video_id}/editing/runs")
def create_run(
    video_id: int,
    body: CreateRunBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    video, project = _video_and_project(video_id, db, current_user, write=True)
    if body.recipe_id not in RECIPES:
        raise HTTPException(status_code=404, detail=f"Unknown recipe {body.recipe_id!r}")
    run = executor.create_run(
        db,
        video=video,
        project=project,
        user_id=current_user.id,
        recipe_id=body.recipe_id,
        params=body.params,
        intent=body.intent,
        selection=body.selection.model_dump() if body.selection else None,
    )
    if body.auto_apply_grant_id:
        grant = (
            db.query(HarnessAutoApplyGrant)
            .filter(
                HarnessAutoApplyGrant.id == body.auto_apply_grant_id,
                HarnessAutoApplyGrant.project_id == project.id,
                HarnessAutoApplyGrant.user_id == current_user.id,
            )
            .first()
        )
        if grant is None:
            raise HTTPException(status_code=404, detail="Auto-apply consent not found")
        run, declined = executor.try_auto_apply(db, run, grant)
        if declined:
            run.warnings = list(run.warnings or []) + [
                f"Not applied automatically: {declined}. Review the plan and apply it yourself."
            ]
            db.commit()
        else:
            job_id = enqueue_harness_apply_job(run.id, run.plan_checksum)
            if job_id is None:
                executor.execute_apply(db, run.id)
                db.refresh(run)
    return _serialize_run(db, run)


class AutoApplyGrantBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipe_id: str = Field(min_length=1, max_length=64)


@router.post("/{video_id}/editing/auto_apply_grants")
def create_auto_apply_grant(
    video_id: int,
    body: AutoApplyGrantBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """One-shot consent to auto-apply a recipe on this video.

    The grant is spent server-side the moment a qualifying run applies under
    it — a replayed create request cannot auto-apply twice on one consent.
    """
    video, project = _video_and_project(video_id, db, current_user, write=True)
    if body.recipe_id not in RECIPES:
        raise HTTPException(status_code=404, detail=f"Unknown recipe {body.recipe_id!r}")
    grant = HarnessAutoApplyGrant(
        project_id=project.id,
        video_id=video.id,
        user_id=current_user.id,
        recipe_id=body.recipe_id,
    )
    db.add(grant)
    db.commit()
    db.refresh(grant)
    return {"id": grant.id, "recipe_id": grant.recipe_id, "created_at": grant.created_at}


@router.get("/{video_id}/editing/runs")
def list_runs(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _video_and_project(video_id, db, current_user, write=False)
    runs = (
        db.query(HarnessRun)
        .filter(HarnessRun.video_id == video_id)
        .order_by(HarnessRun.created_at.desc(), HarnessRun.id.desc())
        .limit(50)
        .all()
    )
    return {
        "runs": [
            {
                "id": run.id,
                "state": run.state,
                "stage": run.stage,
                "recipe_id": run.recipe_id,
                "created_at": run.created_at,
            }
            for run in runs
        ]
    }


@runs_router.get("/{run_id}")
def get_run(
    run_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    run = _run_or_404(run_id, db, current_user, write=False)
    executor.reconcile(db, run)
    return _serialize_run(db, run)


class ToggleOperationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_key: str = Field(min_length=1, max_length=64)
    enabled: bool


@runs_router.patch("/{run_id}/plan")
def patch_plan(
    run_id: int,
    body: ToggleOperationBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    run = _run_or_404(run_id, db, current_user, write=True)
    try:
        executor.set_operation_enabled(db, run, body.operation_key, body.enabled)
    except HarnessError as exc:
        raise _http(exc) from exc
    return _serialize_run(db, run)


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The checksum of the plan the user actually reviewed (plan §13).
    plan_checksum: str = Field(min_length=1, max_length=128)


@runs_router.post("/{run_id}/approve")
def approve_run(
    run_id: int,
    body: ApproveBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    run = _run_or_404(run_id, db, current_user, write=True)
    try:
        executor.approve_run(db, run, reviewed_checksum=body.plan_checksum)
    except HarnessError as exc:
        raise _http(exc) from exc
    return _serialize_run(db, run)


class ApplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The draft revision the client just flushed. A mismatch is a 409, not a
    #: silent overwrite.
    expected_revision: int | None = Field(default=None, ge=0)


@runs_router.post("/{run_id}/apply")
def apply_run(
    run_id: int,
    body: ApplyBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    run = _run_or_404(run_id, db, current_user, write=True)
    try:
        executor.request_apply(db, run, expected_revision=body.expected_revision)
    except HarnessError as exc:
        raise _http(exc) from exc
    job_id = enqueue_harness_apply_job(run.id, run.plan_checksum)
    if job_id is None:
        # No queue configured — run inline (the AI-review pattern), so dev
        # without a worker still works and tests exercise the real body.
        executor.execute_apply(db, run.id)
        db.refresh(run)
    return _serialize_run(db, run)


@runs_router.post("/{run_id}/cancel")
def cancel_run(
    run_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    run = _run_or_404(run_id, db, current_user, write=True)
    try:
        executor.cancel_run(db, run)
    except HarnessError as exc:
        raise _http(exc) from exc
    return _serialize_run(db, run)


@runs_router.post("/{run_id}/revert")
def revert_run(
    run_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    run = _run_or_404(run_id, db, current_user, write=True)
    try:
        executor.revert_run(db, run)
    except HarnessError as exc:
        raise _http(exc) from exc
    return _serialize_run(db, run)


@runs_router.get("/{run_id}/verification")
def get_verification(
    run_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    run = _run_or_404(run_id, db, current_user, write=False)
    return {"verification_report": run.verification_report, "state": run.state}


@runs_router.get("/{run_id}/diff")
def get_diff(
    run_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    run = _run_or_404(run_id, db, current_user, write=False)
    view = draft_store.get_draft(db, run.project_id)
    return {
        "diff": run.diff,
        "base_draft_revision": run.base_draft_revision,
        "current_draft_revision": view.revision,
        "draft_moved": (
            run.base_draft_revision is not None and view.revision != run.base_draft_revision
        ),
    }
