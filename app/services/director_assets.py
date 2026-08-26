"""Turning a plan's shots into actual media.

The interesting part of this module is one string. Twelve shots prompted
independently produce twelve stock photos — different lenses, different light,
different decade — and the montage reads as generated no matter how well each
individual image is chosen. The same twelve behind one specific
director-of-photography brief read as one film. So `house_style_prefix` is
prepended to *every* prompt, identically, and the model is told in the system
prompt not to restate it per shot. This is the single highest-leverage line in
the whole feature and the easiest to quietly drop.

The second thing here is the failure policy: a shot whose generation fails is
**skipped**, never faked and never substituted. An empty or broken clip on the
timeline is worse than an uninterrupted stretch of the speaker's face, because
the user has to find it and remove it. Skips are recorded as warnings so they
are visible rather than silent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.db.models import DirectorPlan, GeneratedMedia
from app.services import ai_media

logger = logging.getLogger(__name__)

#: Appended to every generated prompt. Generation models put text, watermarks
#: and signatures into images unprompted, and any of them makes a shot unusable
#: as B-roll under a talking head.
NEGATIVE_PROMPT = (
    "No text, no captions, no subtitles, no watermark, no logo, no signature, "
    "no borders, no frame, no collage."
)

#: Beyond this share of failures the run is `degraded` rather than `ready`: a
#: montage missing half its shots is not the edit that was planned, and saying
#: so beats quietly shipping a thinner one.
DEGRADED_FAILURE_RATIO = 0.4

#: Terminal states for a generation row.
TERMINAL = {"ready", "failed", "cancelled"}


def compose_prompt(house_style: str, shot: str) -> str:
    """One shot's full prompt: house style, the shot, then the exclusions.

    Order matters. The style leads because it is the thing every shot shares and
    the thing the model should establish first; the exclusions trail because
    they are corrections to the result rather than part of the subject.
    """
    parts = [house_style.strip().rstrip("."), shot.strip().rstrip("."), NEGATIVE_PROMPT]
    return ". ".join(part for part in parts if part)


@dataclass
class AssetRequest:
    """What was asked for, and what came back."""

    created: int = 0
    skipped: list[str] = field(default_factory=list)

    @property
    def any_work(self) -> bool:
        return self.created > 0


def request_assets(db: Any, plan_row: DirectorPlan, plan: dict[str, Any]) -> AssetRequest:
    """Create and enqueue a generation for every shot that needs one.

    Rows are created before anything is enqueued so the UI has something to show
    immediately — a pending tile with a prompt under it is a far better answer to
    "is it doing anything?" than an empty panel and a spinner.

    `project-media` directives are skipped here: they reuse footage that already
    exists, so there is nothing to generate. The compiler resolves them.
    """
    from app.jobs.queue import enqueue_generated_media_job

    brief = plan.get("brief") if isinstance(plan.get("brief"), dict) else {}
    house_style = str(brief.get("houseStylePrefix") or "").strip()
    if not house_style:
        # `validate_plan` refuses a plan without one, so reaching here means the
        # plan was assembled somewhere else. Refuse rather than generate twelve
        # unrelated images.
        raise ValueError("The plan has no house style; generated shots would not cohere")

    result = AssetRequest()
    pending: list[int] = []

    for directive in plan.get("directives") or []:
        if not isinstance(directive, dict):
            continue
        asset = directive.get("asset") if isinstance(directive.get("asset"), dict) else {}
        source = str(asset.get("source") or "")
        if not source.startswith("generate"):
            continue

        shot = str(asset.get("prompt") or "").strip()
        if not shot:
            result.skipped.append(str(directive.get("id") or "?"))
            continue

        kind = "video" if source == "generate-video" else "image"
        row = GeneratedMedia(
            project_id=plan_row.project_id,
            video_id=plan_row.video_id,
            user_id=plan_row.user_id,
            kind=kind,
            prompt=compose_prompt(house_style, shot),
            model=(
                ai_media.DEFAULT_VIDEO_MODEL if kind == "video" else ai_media.DEFAULT_IMAGE_MODEL
            ),
            aspect_ratio=str(asset.get("aspectRatio") or "16:9"),
            duration_seconds=(
                float(asset.get("durationSeconds") or 0) or None if kind == "video" else None
            ),
            status="pending",
            # Generated media is normally reviewed before it joins the media
            # panel. The director's output is reviewed as a *plan*, so these
            # arrive already accepted — otherwise the user would approve the
            # same shots twice.
            saved=True,
            director_plan_id=plan_row.id,
            director_directive_id=str(directive.get("id") or ""),
        )
        db.add(row)
        result.created += 1
        pending.append(row.id)

    db.commit()

    for row in (
        db.query(GeneratedMedia).filter(GeneratedMedia.director_plan_id == plan_row.id).all()
    ):
        if row.status == "pending":
            enqueue_generated_media_job(row.id)

    logger.info(
        "Director plan %s requested %s asset(s), skipped %s",
        plan_row.id,
        result.created,
        len(result.skipped),
    )
    return result


def reconcile(db: Any, plan_row: DirectorPlan) -> bool:
    """Advance a generating run once its assets have all settled.

    Called from the API's poll rather than from a worker holding a slot open for
    half an hour. The same shape as `_reconcile_dead_effect` in the AI routes,
    and for the same reason: a row nothing ever advances sits at a percentage
    that will never move again.

    Returns True when the row changed.
    """
    if plan_row.status != "generating":
        return False

    assets = (
        db.query(GeneratedMedia).filter(GeneratedMedia.director_plan_id == plan_row.id).all()
    )
    if not assets:
        plan_row.status = "ready"
        plan_row.stage = "Ready"
        plan_row.progress = 100
        db.commit()
        return True

    settled = [asset for asset in assets if asset.status in TERMINAL]
    ready = [asset for asset in assets if asset.status == "ready"]
    failed = [asset for asset in assets if asset.status in {"failed", "cancelled"}]

    if len(settled) < len(assets):
        # Progress spans the generating stage only — planning already claimed
        # the first quarter, and a bar that restarts at zero reads as a stall.
        plan_row.stage = f"Sourcing {len(assets)} shots ({len(ready)} ready)"
        plan_row.progress = 25 + int(70 * len(settled) / len(assets))
        db.commit()
        return True

    warnings = list(plan_row.warnings or [])
    if failed:
        warnings.append(
            f"{len(failed)} shot(s) could not be generated and were left out of the edit."
        )

    degraded = len(failed) / len(assets) > DEGRADED_FAILURE_RATIO
    plan_row.status = "degraded" if degraded else "ready"
    plan_row.stage = (
        "Some shots could not be made" if degraded else f"Ready — {len(ready)} shot(s)"
    )
    plan_row.progress = 100
    plan_row.warnings = warnings
    db.commit()
    logger.info(
        "Director plan %s finished generating: %s ready, %s failed%s",
        plan_row.id,
        len(ready),
        len(failed),
        " (degraded)" if degraded else "",
    )
    return True
