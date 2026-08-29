"""Run lifecycle for the editing harness: plan → approve → stage → commit → verify → revert.

The seam the Director proved, kept: everything that mutates a draft dict is in
`mutations` (pure); this module owns state transitions and the database.

Two-phase execution (plan §14): Phase A stages every asset a plan needs — for
the subject mask that is a real `rough_cut_effect` row run through the exact
machinery the inspector button uses, so cancellation, progress, publishing and
export approval are inherited rather than re-implemented. Phase B commits the
deterministic mutation through `draft_store` under an `expected_revision`
compare; a mismatch is a `conflicted` run, never a silent overwrite.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import AiResult, HarnessOperation, HarnessRun, Project, Video
from app.services import draft_store
from app.services.draft_store import DraftConflict
from app.services.harness import capabilities as caps
from app.services.harness.compiler import CompileError, compile_recipe, estimate_plan
from app.services.harness.mutations import MutationContext, apply_plan, revert_manifest
from app.services.harness.schemas import HarnessPlan, entity_id, plan_checksum
from app.services.harness.verifier import verify_committed

logger = logging.getLogger(__name__)

#: A run in `staging`/`applying` whose row has not moved for this long is
#: presumed dead (worker killed) and failed by `reconcile` — the liveness rule
#: the effect jobs already have and every other job lacked (plan §15).
STALE_ACTIVE_SECONDS = 900

ACTIVE_STATES = {"staging", "applying", "verifying"}
APPLICABLE_STATES = {"planned", "approved"}

# Patched in tests; the real thing shells out to ffmpeg/segmentation.
from app.jobs.rough_cut_effect import rough_cut_effect_job as _run_effect_job  # noqa: E402


class HarnessError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _plan_from_row(run: HarnessRun) -> HarnessPlan:
    return HarnessPlan.model_validate(run.plan)


def _sync_operation_rows(db: Session, run: HarnessRun, plan: HarnessPlan) -> None:
    existing = {
        op.operation_key: op
        for op in db.query(HarnessOperation).filter(HarnessOperation.run_id == run.id).all()
    }
    for sequence, op in enumerate(plan.operations):
        row = existing.get(op.id)
        payload = op.model_dump(mode="json")
        if row is None:
            row = HarnessOperation(
                run_id=run.id,
                operation_key=op.id,
                idempotency_key=f"ehr{run.id}:{op.id}:v{op.schemaVersion}",
            )
            db.add(row)
        row.type = op.type
        row.schema_version = op.schemaVersion
        row.sequence = sequence
        row.depends_on = list(op.dependsOn)
        row.state = "pending" if op.enabled else "disabled"
        row.risk = op.risk
        row.params = payload
    db.flush()


def _operation_rows(db: Session, run: HarnessRun) -> list[HarnessOperation]:
    return (
        db.query(HarnessOperation)
        .filter(HarnessOperation.run_id == run.id)
        .order_by(HarnessOperation.sequence.asc())
        .all()
    )


def _mutation_context(
    db: Session, run: HarnessRun, staged_assets: dict[str, dict[str, Any]] | None = None
) -> MutationContext:
    video = db.query(Video).filter(Video.id == run.video_id).first()
    return MutationContext(
        run_id=run.id,
        video_id=run.video_id,
        video_duration=float(getattr(video, "duration", 0) or 0),
        source_url=str(getattr(video, "file_path", "") or "") or None,
        staged_assets=staged_assets or {},
    )


def _fail(db: Session, run: HarnessRun, code: str, message: str) -> None:
    run.state = "failed"
    run.error_code = code
    run.error_detail = message[:2000]
    run.stage = None
    db.commit()


# -- planning ------------------------------------------------------------------


def create_run(
    db: Session,
    *,
    video: Video,
    project: Project,
    user_id: int | None,
    recipe_id: str,
    params: dict[str, Any],
    intent: str | None = None,
    selection: dict[str, float] | None = None,
    request_id: str | None = None,
) -> HarnessRun:
    """Create, compile, and simulate a run in one step.

    Two ways in: explicit `params` (the deterministic quick-action path), or a
    natural-language `intent` with no params — then the planner chain proposes
    parameters, and everything it proposes still goes through the same
    `compile_recipe` validation. A deployment with no planning model does not
    fail cryptically; the run lands in `needs_input` with the sentence naming
    what to configure.
    """
    snapshot = caps.snapshot()
    run = HarnessRun(
        project_id=project.id,
        video_id=video.id,
        workspace_id=project.workspace_id,
        created_by=user_id,
        state="planning",
        intent=intent,
        recipe_id=recipe_id,
        params=params,
        capability_snapshot=snapshot,
        request_id=request_id,
    )
    db.add(run)
    db.flush()

    view = draft_store.get_draft(db, project.id)
    video_duration = float(getattr(video, "duration", 0) or 0) or float(
        view.payload.get("sourceDuration") or 0
    )

    plan_params = dict(params or {})
    if not plan_params and intent:
        from app.services.harness.planner import (
            PROMPT_VERSION,
            PlannerError,
            plan_recipe_params,
        )

        seg = caps.capability(snapshot, "segmentation")
        max_clip = float((seg.get("limits") or {}).get("maxClipSeconds") or 120)
        run.stage = "Reading the request"
        db.commit()
        try:
            planned = plan_recipe_params(
                intent,
                video_duration=video_duration,
                max_clip_seconds=max_clip,
                selection=selection,
            )
        except PlannerError as exc:
            run.state = "needs_input"
            run.error_code = "planner_unavailable"
            run.error_detail = str(exc)[:2000]
            run.stage = "Could not plan from the description"
            db.commit()
            return run
        plan_params = planned.params
        run.params = plan_params
        run.model_provider = (
            "anthropic" if "claude" in planned.model.lower() else "openrouter"
        )
        run.model_name = planned.model
        run.prompt_version = PROMPT_VERSION
        run.token_usage = planned.usage

    try:
        plan = compile_recipe(
            recipe_id,
            plan_params,
            capability_snapshot=snapshot,
            video_duration=video_duration,
        )
    except CompileError as exc:
        _fail(db, run, exc.code, str(exc))
        return run

    # Simulate against the base revision — the diff the user reviews.
    ctx = _mutation_context(db, run)
    simulated = apply_plan(view.payload, plan, ctx)

    run.plan = plan.model_dump(mode="json")
    run.plan_checksum = plan_checksum(plan)
    run.recipe_version = plan.recipeVersion
    run.base_draft_revision = view.revision
    run.base_checksum = view.checksum
    run.warnings = list(plan.warnings)
    run.estimates = estimate_plan(plan)
    run.diff = {
        "manifest": simulated.manifest,
        "warnings": simulated.warnings,
        "operationCount": len(plan.operations),
    }
    run.state = "planned"
    run.stage = f"Planned — {len(plan.operations)} step(s)"
    run.planned_at = _now()
    _sync_operation_rows(db, run, plan)
    db.commit()
    return run


def set_operation_enabled(
    db: Session, run: HarnessRun, operation_key: str, enabled: bool
) -> HarnessRun:
    """Toggle one operation; dependents of a disabled operation disable too."""
    if run.state not in {"planned", "approved"}:
        raise HarnessError("not_editable", f"A {run.state} run's plan cannot be edited.")
    plan = _plan_from_row(run)
    keyed = {op.id: op for op in plan.operations}
    if operation_key not in keyed:
        raise HarnessError("unknown_operation", f"No operation {operation_key!r} in this plan.")

    keyed[operation_key].enabled = enabled
    if not enabled:
        # Cascade: anything that depends on (or targets) a disabled op disables.
        changed = True
        while changed:
            changed = False
            for op in plan.operations:
                if not op.enabled:
                    continue
                deps = set(op.dependsOn) | (
                    {getattr(op, "targetOp")} if getattr(op, "targetOp", None) else set()
                )
                if any(not keyed[d].enabled for d in deps if d in keyed):
                    op.enabled = False
                    changed = True

    view = draft_store.get_draft(db, run.project_id)
    simulated = apply_plan(view.payload, plan, _mutation_context(db, run))
    run.plan = plan.model_dump(mode="json")
    run.plan_checksum = plan_checksum(plan)
    run.diff = {
        "manifest": simulated.manifest,
        "warnings": simulated.warnings,
        "operationCount": sum(1 for op in plan.operations if op.enabled),
    }
    # Editing the plan voids a previous approval: what was reviewed changed.
    run.state = "planned"
    run.approved_at = None
    _sync_operation_rows(db, run, plan)
    db.commit()
    return run


def approve_run(db: Session, run: HarnessRun, *, reviewed_checksum: str) -> HarnessRun:
    if run.state != "planned":
        raise HarnessError("not_approvable", f"A {run.state} run cannot be approved.")
    if reviewed_checksum != run.plan_checksum:
        raise HarnessError(
            "stale_plan",
            "The plan changed since it was reviewed; re-read it before approving.",
        )
    run.state = "approved"
    run.approved_at = _now()
    run.stage = "Approved"
    db.commit()
    return run


# -- staging + commit ----------------------------------------------------------


def _stage_subject_mask(
    db: Session, run: HarnessRun, op_row: HarnessOperation, plan: HarnessPlan
) -> dict[str, Any]:
    """Phase A for `visual.apply_subject_mask`: a real effect row, run inline.

    Reusing `rough_cut_effect_job` inherits publishing, cancellation semantics,
    and — critically — the export path's effect-row approval checks
    (`_approved_timeline_layers` verifies effectType + clipKey against this
    exact row, so the composite survives into the MP4).
    """
    op = next(o for o in plan.operations if o.id == op_row.operation_key)
    target = next(o for o in plan.operations if o.id == op.targetOp)
    item_id = entity_id(run.id, target.id)
    clip_key = f"media:{item_id}"

    existing_id = (op_row.staged_asset or {}).get("resultId")
    if existing_id:
        row = db.query(AiResult).filter(AiResult.id == existing_id).first()
    else:
        row = AiResult(
            video_id=run.video_id,
            result_type="rough_cut_effect",
            status="queued",
            result_data={
                "effectType": "remove_bg",
                "clipKey": clip_key,
                "clipTarget": {
                    "track": "video",
                    "start": target.range.start,
                    "end": target.range.end,
                },
                "settings": {"autoRemoval": True, "quality": op.quality},
                "status": "queued",
                "progress": 0,
                "harnessRunId": run.id,
            },
        )
        db.add(row)
        db.flush()
        op_row.staged_asset = {"resultId": row.id}
        op_row.job_id = f"harness-{run.id}-{op_row.operation_key}-{row.id}"
        db.commit()

    if row is None:
        raise HarnessError("staging_lost", "The staged effect row disappeared.")

    if row.status not in {"completed", "failed", "canceled"}:
        _run_effect_job(row.id)
        db.expire(row)

    data = row.result_data if isinstance(row.result_data, dict) else {}
    if row.status == "completed" and data.get("outputUrl"):
        return {"resultId": row.id, "outputUrl": data.get("outputUrl")}
    raise HarnessError(
        "staging_failed",
        str(data.get("error") or row.error_message or "Background removal failed."),
    )


def request_apply(
    db: Session, run: HarnessRun, *, expected_revision: int | None
) -> HarnessRun:
    """Move an approved run into staging and record the revision to commit against.

    The caller (route) enqueues `harness_apply_job`, or runs it inline when no
    queue is configured — the AI-review pattern.
    """
    if run.state not in APPLICABLE_STATES:
        raise HarnessError("not_applicable", f"A {run.state} run cannot be applied.")
    view = draft_store.get_draft(db, run.project_id)
    if expected_revision is not None and expected_revision != view.revision:
        raise HarnessError(
            "draft_moved",
            f"The draft is at revision {view.revision}, not {expected_revision}. "
            "Reload the editor and retry.",
        )
    run.base_draft_revision = view.revision
    run.base_checksum = view.checksum
    run.state = "staging"
    run.stage = "Preparing effects"
    run.error_code = None
    run.error_detail = None
    db.commit()
    return run


def execute_apply(db: Session, run_id: int) -> None:
    """The staging + commit + verify body. Runs in a worker or inline."""
    run = db.query(HarnessRun).filter(HarnessRun.id == run_id).first()
    if run is None:
        raise RuntimeError(f"Harness run {run_id} was removed before applying")
    if run.state != "staging":
        logger.info("Harness run %s is %s; apply skipped", run_id, run.state)
        return
    try:
        plan = _plan_from_row(run)
    except Exception as exc:  # noqa: BLE001
        _fail(db, run, "invalid_plan", f"The stored plan no longer validates: {exc}")
        return

    op_rows = {row.operation_key: row for row in _operation_rows(db, run)}
    staged_assets: dict[str, dict[str, Any]] = {}
    enabled_ops = [op for op in plan.operations if op.enabled]

    # Phase A — stage.
    for op in enabled_ops:
        if run.cancel_requested:
            run.state = "cancelled"
            run.stage = "Cancelled before anything changed"
            db.commit()
            return
        row = op_rows.get(op.id)
        if row is None:
            continue
        if op.type == "visual.apply_subject_mask":
            row.state = "staging"
            row.started_at = _now()
            run.stage = "Cutting out the subject"
            db.commit()
            try:
                staged = _stage_subject_mask(db, run, row, plan)
            except HarnessError as exc:
                row.state = "failed"
                row.error_code = exc.code
                row.error_detail = str(exc)[:2000]
                db.commit()
                if getattr(op, "required", True):
                    _fail(
                        db, run, exc.code,
                        f"{exc} The timeline was not changed.",
                    )
                    return
                staged = {}
            row.staged_asset = {**(row.staged_asset or {}), **staged}
            row.state = "staged"
            db.commit()
            staged_assets[op.id] = staged

    # Phase B — commit, atomically, against the recorded revision.
    run.state = "applying"
    run.stage = "Placing the edit"
    db.commit()

    ctx = _mutation_context(db, run, staged_assets)
    mutated: dict[str, Any] = {}

    def _mutator(payload: dict[str, Any]) -> dict[str, Any]:
        result = apply_plan(payload, plan, ctx)
        mutated["result"] = result
        return result.draft

    try:
        view = draft_store.get_draft(db, run.project_id)
        if run.base_draft_revision is not None and view.revision != run.base_draft_revision:
            run.state = "conflicted"
            run.stage = "The draft changed while effects were rendering"
            run.error_code = "draft_moved"
            run.error_detail = (
                f"Draft moved from revision {run.base_draft_revision} to {view.revision} "
                "during staging. Nothing was changed — re-plan against the new state."
            )
            db.commit()
            return
        new_view = draft_store.save_draft(
            db,
            run.project_id,
            _mutator(view.payload),
            writer=f"harness:{run.id}",
            expected_revision=view.revision,
            video_id=run.video_id,
            source_id=f"ehr{run.id}",
        )
    except DraftConflict as conflict:
        db.rollback()
        run = db.query(HarnessRun).filter(HarnessRun.id == run_id).first()
        run.state = "conflicted"
        run.error_code = "draft_moved"
        run.error_detail = str(conflict)
        db.commit()
        return

    result = mutated["result"]
    run.state = "verifying"
    run.stage = "Checking the result"
    run.applied_draft_revision = new_view.revision
    run.result_checksum = new_view.checksum
    run.applied_manifest = result.manifest
    run.inverse_manifest = result.inverse
    run.warnings = list(run.warnings or []) + result.warnings
    run.applied_at = _now()
    for op in enabled_ops:
        row = op_rows.get(op.id)
        if row is not None and row.state != "failed":
            row.state = "applied"
            row.completed_at = _now()
    db.commit()

    report = verify_committed(new_view.payload, run, plan)
    run.verification_report = report
    run.verified_at = _now()
    if report.get("status") == "fail":
        run.state = "failed"
        run.error_code = "verification_failed"
        run.error_detail = "; ".join(
            c.get("detail", c.get("check", "")) for c in report.get("checks", [])
            if c.get("status") == "fail"
        )[:2000]
        run.stage = "Applied, but verification failed — revert is available"
    else:
        run.state = "ready"
        run.stage = "On the timeline"
    db.commit()


# -- revert / cancel / reconcile ----------------------------------------------


def revert_run(db: Session, run: HarnessRun) -> HarnessRun:
    if run.state not in {"ready", "failed", "partially_applied"} or not run.inverse_manifest:
        raise HarnessError("not_reverted", f"A {run.state} run has nothing to revert.")

    warnings_box: dict[str, list[str]] = {}

    def _mutator(payload: dict[str, Any]) -> dict[str, Any]:
        reverted, warnings = revert_manifest(payload, run.inverse_manifest or [])
        warnings_box["warnings"] = warnings
        return reverted

    draft_store.mutate_draft(
        db,
        run.project_id,
        _mutator,
        writer=f"harness:{run.id}",
        video_id=run.video_id,
        source_id=f"ehr{run.id}:revert",
    )
    run.state = "reverted"
    run.stage = "Taken back off the timeline"
    run.reverted_at = _now()
    # The inverse manifest is retained deliberately — the audit trail the
    # Director destroyed on revert (plan §12.2).
    run.warnings = list(run.warnings or []) + warnings_box.get("warnings", [])
    db.commit()
    return run


def cancel_run(db: Session, run: HarnessRun) -> HarnessRun:
    if run.state in {"ready", "reverted"}:
        raise HarnessError("not_cancellable", "This run already finished; use revert.")
    run.cancel_requested = True
    if run.state not in ACTIVE_STATES:
        run.state = "cancelled"
        run.stage = "Cancelled"
    db.commit()
    return run


def reconcile(db: Session, run: HarnessRun) -> HarnessRun:
    """Advance or fail a run whose worker died — the liveness rule on GET."""
    if run.state in ACTIVE_STATES and run.updated_at is not None:
        updated = run.updated_at
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        age = (_now() - updated).total_seconds()
        if age > STALE_ACTIVE_SECONDS:
            _fail(
                db, run, "worker_died",
                f"No progress for {int(age)}s — the worker likely stopped. "
                "The draft was not changed unless the run reached 'ready'.",
            )
    return run
