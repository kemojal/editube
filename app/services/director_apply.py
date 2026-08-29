"""Landing a finished plan on the draft, and taking it back off.

Kept apart from `director_compile` because the two answer different questions.
The compiler side (`resolve_placements`) is pure — a draft and a plan in,
placements out — which is what makes it testable against a fixture and
reproducible by the editor's own replay. This module is the part that touches
the database: reading the draft, resolving which assets actually finished, and
writing the result back — since plan Phase 4, *through the harness engine*.
Each apply mints a `HarnessRun` that owns the compiled `timeline.place_media`
plan, the applied and inverse manifests, and the operation rows; revert routes
through `executor.revert_run` and keeps the inverse as an audit trail.

Worth stating plainly: **applying only ever adds.** Existing clips, attributes
and tracks are carried through untouched, so applying a plan cannot destroy
someone's editing even if they have been working in the meantime. Reverting
replays the run's inverse manifest, whose `restore_value` entries refuse to
clobber anything the user changed after the run. Pre-migration manifests (no
`harnessRunId`) keep the old id-filter revert so existing applied runs stay
revertible.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.db.models import DirectorPlan, GeneratedMedia
from app.services.director_compile import revert_plan
from app.services.director_context import build_context

logger = logging.getLogger(__name__)

#: Statuses a plan can be applied from. `degraded` is included deliberately: a
#: run whose assets partly failed still produced real shots, and the user may
#: reasonably want the ones that worked.
APPLICABLE = {"ready", "degraded"}


class NotApplicable(RuntimeError):
    """The run is not in a state where it can be applied."""


@dataclass
class Applied:
    placed: int
    warnings: list[str]


def _op_id(directive_id: str) -> str:
    """A directive id as a schema-legal harness operation id."""
    cleaned = re.sub(r"[^a-z0-9_]", "", directive_id.lower())
    return f"shot_{cleaned or 'x'}"


def _placements_to_plan(placements: list[Any]) -> "HarnessPlan":
    """Resolved Director placements → a harness plan of `timeline.place_media`."""
    from app.services.harness.schemas import HarnessPlan, Keyframe, PlaceMediaOp

    operations = []
    seen: set[str] = set()
    for placement in placements:
        op_id = _op_id(placement.directive_id)
        while op_id in seen:  # pragma: no cover — validated directive ids are unique
            op_id += "x"
        seen.add(op_id)
        operations.append(
            PlaceMediaOp(
                id=op_id,
                name=placement.name,
                kind=placement.kind,  # type: ignore[arg-type]
                sourceId=placement.asset_id,
                sourceUrl=placement.asset_url,
                assetDuration=placement.asset_duration,
                startSec=placement.source_start,
                endSec=placement.source_end,
                playDuration=placement.play_duration,
                trackLabel="V2",
                animation=placement.animation,
                keyframes={
                    channel: [Keyframe.model_validate(kf) for kf in track]
                    for channel, track in (placement.keyframes or {}).items()
                }
                or None,
                explanationKey="harness.op.place_broll",
            )
        )
    return HarnessPlan(recipe="director_broll", operations=operations)


def apply_plan(db: Any, plan_row: DirectorPlan) -> Applied:
    """Apply the Director's plan THROUGH the harness engine (plan Phase 4).

    The Director keeps its own UX state machine; the mutation, the inverse
    manifest, the revision safety and the revert all belong to the harness
    now — one engine, not two. Resolution (anchors → placements) still runs
    against the draft *as it is at apply time*, inside the store's
    conflict-retry loop; the placements become `timeline.place_media`
    operations on a run-id-addressed `HarnessRun`, whose inverse manifest
    survives revert as the audit trail the old path destroyed.
    """
    if plan_row.status not in APPLICABLE:
        raise NotApplicable(f"A {plan_row.status} run cannot be applied")
    plan = plan_row.plan if isinstance(plan_row.plan, dict) else None
    if not plan:
        raise NotApplicable("This run has no plan to apply")

    from app.db.models import HarnessRun, Video, VideoTranscription
    from app.services import draft_store
    from app.services.director_compile import resolve_placements
    from app.services.harness import executor as harness_executor
    from app.services.harness.mutations import MutationContext, apply_plan as apply_harness_plan
    from app.services.harness.schemas import plan_checksum
    from app.services.harness.verifier import verify_committed

    video = db.query(Video).filter(Video.id == plan_row.video_id).first()
    transcription = (
        db.query(VideoTranscription)
        .filter(VideoTranscription.video_id == plan_row.video_id)
        .first()
    )
    segments = transcription.segments if transcription and transcription.segments else []

    assets = {
        str(asset.director_directive_id): asset
        for asset in db.query(GeneratedMedia)
        .filter(GeneratedMedia.director_plan_id == plan_row.id)
        .all()
        if asset.director_directive_id
    }

    run = HarnessRun(
        project_id=plan_row.project_id,
        video_id=plan_row.video_id,
        created_by=plan_row.user_id,
        state="applying",
        stage="Placing the director's shots",
        intent=f"Director plan {plan_row.id}",
        recipe_id="director_broll",
        params={"directorPlanId": plan_row.id},
    )
    db.add(run)
    db.flush()

    box: dict[str, Any] = {}
    # Captured once: the store's conflict-retry loop may run the mutator more
    # than once, and appending to the row's own list each time would duplicate.
    base_warnings = list(plan_row.warnings or [])
    now = datetime.now(timezone.utc)

    def _mutator(draft: dict[str, Any]) -> dict[str, Any]:
        # Rebuilt from the draft *as it is now*, not as it was when the plan
        # was written. If the user has re-cut since, the anchors resolve
        # against the current cut — which is the whole reason shots are
        # anchored to words rather than to timestamps.
        context = build_context(
            segments=segments,
            keep_ranges=draft.get("keepRanges") or [],
            source_duration=float(getattr(video, "duration", 0) or 0)
            or float(draft.get("sourceDuration") or 0),
            aspect=str((draft.get("layoutStyle") or {}).get("aspect") or "16:9"),
        )
        placements, warnings = resolve_placements(
            plan, context=context, assets_by_directive=assets
        )

        if placements:
            hplan = _placements_to_plan(placements)
            ctx = MutationContext(
                run_id=run.id,
                video_id=plan_row.video_id,
                video_duration=float(getattr(video, "duration", 0) or 0),
            )
            result = apply_harness_plan(draft, hplan, ctx)
            mutated = result.draft
            warnings = warnings + result.warnings
        else:
            hplan = None
            result = None
            mutated = dict(draft)

        # The run stamps the draft; the inverse restores its absence exactly.
        inverse = list(result.inverse) if result else []
        for key in ("directorPlanId", "directorAppliedAt"):
            inverse.append(
                {
                    "op": "restore_value",
                    "path": [key],
                    "before": mutated[key] if key in mutated else {"__absent__": True},
                    "after": plan_row.id if key == "directorPlanId" else now.isoformat(),
                }
            )
        mutated["directorPlanId"] = plan_row.id
        mutated["directorAppliedAt"] = now.isoformat()

        placed = len(result.created_item_ids) if result else 0
        box.update(
            hplan=hplan, result=result, placed=placed, warnings=warnings, inverse=inverse
        )

        # Same session: everything lands in the store's commit, atomically
        # with the draft write — a crash cannot leave an applied draft with
        # no manifest on either row.
        run.plan = hplan.model_dump(mode="json") if hplan else None
        run.plan_checksum = plan_checksum(hplan) if hplan else None
        run.applied_manifest = result.manifest if result else {
            "timelineMediaItemIds": [], "textOverlayIds": [],
            "trackIds": [], "clipAttributeKeys": [],
        }
        run.inverse_manifest = inverse
        run.warnings = warnings
        run.state = "ready"
        run.stage = f"Applied — {placed} shot(s) on the timeline"
        run.applied_at = now

        plan_row.applied_manifest = {
            "harnessRunId": run.id,
            **(result.manifest if result else {}),
        }
        plan_row.applied_at = now
        plan_row.status = "applied"
        plan_row.stage = f"Applied — {placed} shot(s) on the timeline"
        plan_row.warnings = base_warnings + warnings
        return mutated

    saved = draft_store.mutate_draft(
        db,
        plan_row.project_id,
        _mutator,
        writer="director",
        video_id=plan_row.video_id,
        source_id=f"director:{plan_row.id}",
    )

    run.applied_draft_revision = saved.revision if saved else None
    run.result_checksum = saved.checksum if saved else None
    if saved and box.get("hplan") is not None:
        run.verification_report = verify_committed(saved.payload, run, box["hplan"])
        run.verified_at = datetime.now(timezone.utc)
    db.commit()

    _sync_run_operations(db, harness_executor, run, box.get("hplan"))

    logger.info(
        "Applied director plan %s to video %s via harness run %s: %s shot(s)",
        plan_row.id,
        plan_row.video_id,
        run.id,
        box.get("placed", 0),
    )
    return Applied(placed=box.get("placed", 0), warnings=box.get("warnings", []))


def _sync_run_operations(db: Any, harness_executor: Any, run: Any, hplan: Any) -> None:
    if hplan is None:
        return
    harness_executor._sync_operation_rows(db, run, hplan)
    for row in harness_executor._operation_rows(db, run):
        row.state = "applied"
    db.commit()


def revert(db: Any, plan_row: DirectorPlan) -> bool:
    """Take the run's shots back off the timeline.

    New runs revert through their harness run's inverse manifest — which
    SURVIVES the revert as an audit trail (the old path nulled it). The
    DirectorPlan row still resets to `ready` so the run can be re-applied
    without re-planning or re-generating anything; the next apply mints a
    fresh harness run. Pre-migration manifests (no `harnessRunId`) keep the
    legacy id-filter path so old applied runs stay revertible.
    """
    manifest = plan_row.applied_manifest if isinstance(plan_row.applied_manifest, dict) else None
    if plan_row.status != "applied" or not manifest:
        return False

    from app.services import draft_store

    harness_run_id = manifest.get("harnessRunId")
    if harness_run_id:
        from app.db.models import HarnessRun
        from app.services.harness import executor as harness_executor

        run = db.query(HarnessRun).filter(HarnessRun.id == harness_run_id).first()
        if run is not None and run.state == "ready":
            # Set first: the harness revert's commit lands these atomically.
            plan_row.status = "ready"
            plan_row.stage = "Reverted"
            plan_row.applied_manifest = None
            plan_row.applied_at = None
            harness_executor.revert_run(db, run)
            logger.info(
                "Reverted director plan %s via harness run %s",
                plan_row.id,
                harness_run_id,
            )
            return True
        # The harness run is gone or already reverted — reset the Director row
        # so the UI is not stuck, but change no draft state we cannot verify.
        plan_row.status = "ready"
        plan_row.stage = "Reverted"
        plan_row.applied_manifest = None
        plan_row.applied_at = None
        db.commit()
        return True

    def _mutator(draft: dict[str, Any]) -> dict[str, Any]:
        # Back to the state it was in before applying, so it can be applied
        # again without re-planning or re-generating anything.
        plan_row.status = "ready"
        plan_row.stage = "Reverted"
        plan_row.applied_manifest = None
        plan_row.applied_at = None
        return revert_plan(draft, manifest)

    draft_store.mutate_draft(
        db,
        plan_row.project_id,
        _mutator,
        writer="director",
        video_id=plan_row.video_id,
        source_id=f"director:{plan_row.id}:revert",
    )

    logger.info("Reverted director plan %s from video %s", plan_row.id, plan_row.video_id)
    return True
