"""Landing a finished plan on the draft, and taking it back off.

Kept apart from `director_compile` because the two answer different questions.
The compiler is pure — a draft and a plan in, a draft out — which is what makes
it testable against a fixture and reproducible by the editor's own replay. This
module is the part that touches the database: reading the draft, resolving which
assets actually finished, and writing the result back.

Worth stating plainly: **compiling only ever adds.** Existing clips, attributes
and tracks are carried through untouched, so applying a plan cannot destroy
someone's editing even if they have been working in the meantime. That is why
there is no snapshot column here — reverting is a filter over the ids the run
recorded, not a restore of a saved copy, and it therefore leaves anything the
user did since exactly where it is.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.db.models import AiResult, DirectorPlan, GeneratedMedia
from app.services.director_compile import compile_plan, revert_plan
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


def _draft_row(db: Any, video_id: int) -> AiResult:
    row = (
        db.query(AiResult)
        .filter(AiResult.video_id == video_id, AiResult.result_type == "rough_cut_draft")
        .first()
    )
    if row is None:
        row = AiResult(video_id=video_id, result_type="rough_cut_draft", status="completed")
        db.add(row)
        db.flush()
    return row


def apply_plan(db: Any, plan_row: DirectorPlan) -> Applied:
    """Compile the plan into the video's rough-cut draft."""
    if plan_row.status not in APPLICABLE:
        raise NotApplicable(f"A {plan_row.status} run cannot be applied")
    plan = plan_row.plan if isinstance(plan_row.plan, dict) else None
    if not plan:
        raise NotApplicable("This run has no plan to apply")

    draft_row = _draft_row(db, plan_row.video_id)
    draft = dict(draft_row.result_data or {}) if isinstance(draft_row.result_data, dict) else {}

    from app.db.models import Video, VideoTranscription

    video = db.query(Video).filter(Video.id == plan_row.video_id).first()
    transcription = (
        db.query(VideoTranscription)
        .filter(VideoTranscription.video_id == plan_row.video_id)
        .first()
    )
    segments = transcription.segments if transcription and transcription.segments else []

    # Rebuilt from the draft *as it is now*, not as it was when the plan was
    # written. If the user has re-cut since, the anchors resolve against the
    # current cut — which is the whole reason shots are anchored to words
    # rather than to timestamps.
    context = build_context(
        segments=segments,
        keep_ranges=draft.get("keepRanges") or [],
        source_duration=float(getattr(video, "duration", 0) or 0)
        or float(draft.get("sourceDuration") or 0),
        aspect=str((draft.get("layoutStyle") or {}).get("aspect") or "16:9"),
    )

    assets = {
        str(asset.director_directive_id): asset
        for asset in db.query(GeneratedMedia)
        .filter(GeneratedMedia.director_plan_id == plan_row.id)
        .all()
        if asset.director_directive_id
    }

    compiled = compile_plan(
        draft,
        plan,
        context=context,
        assets_by_directive=assets,
        plan_id=plan_row.id,
        applied_at=datetime.now(timezone.utc).isoformat(),
    )

    draft_row.result_data = compiled.draft
    draft_row.status = "completed"
    plan_row.applied_manifest = compiled.manifest
    plan_row.applied_at = datetime.now(timezone.utc)
    plan_row.status = "applied"
    plan_row.stage = f"Applied — {compiled.placed} shot(s) on the timeline"
    plan_row.warnings = list(plan_row.warnings or []) + compiled.warnings
    db.commit()

    logger.info(
        "Applied director plan %s to video %s: %s shot(s)",
        plan_row.id,
        plan_row.video_id,
        compiled.placed,
    )
    return Applied(placed=compiled.placed, warnings=compiled.warnings)


def revert(db: Any, plan_row: DirectorPlan) -> bool:
    """Remove everything the run put on the timeline.

    A filter over the recorded manifest rather than a restore: the user has very
    likely edited since, and a restore would take their work with it.
    """
    manifest = plan_row.applied_manifest if isinstance(plan_row.applied_manifest, dict) else None
    if plan_row.status != "applied" or not manifest:
        return False

    draft_row = _draft_row(db, plan_row.video_id)
    draft = dict(draft_row.result_data or {}) if isinstance(draft_row.result_data, dict) else {}

    draft_row.result_data = revert_plan(draft, manifest)
    # Back to the state it was in before applying, so it can be applied again
    # without re-planning or re-generating anything.
    plan_row.status = "ready"
    plan_row.stage = "Reverted"
    plan_row.applied_manifest = None
    plan_row.applied_at = None
    db.commit()

    logger.info("Reverted director plan %s from video %s", plan_row.id, plan_row.video_id)
    return True
