"""Deterministic recipe compilers: parameters in, a primitive plan out.

No model call anywhere in this module — the first milestone proves the harness
with deterministic recipes (plan §28), and a model-assisted planner only ever
*proposes* the same validated parameter shapes these compilers consume.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.services.harness.schemas import (
    ANIMATION_PRESETS,
    TEXT_TEMPLATES,
    AdjustClipOp,
    CreateTextOp,
    DuplicateLinkedOp,
    HarnessPlan,
    SourceRange,
    SubjectMaskOp,
)
from app.services.harness.capabilities import capability

RECIPES: dict[str, dict[str, Any]] = {
    "subject_behind_text": {
        "version": 1,
        "title": "Subject behind text",
        "description": "Duplicate the subject above a title so the text passes behind them.",
        "requires": ["segmentation"],
    },
    "review_fix": {
        "version": 1,
        "title": "Apply a review fix",
        "description": "A bounded correction from an AI-review finding, on the clip it flagged.",
        "requires": [],
    },
}


class CompileError(ValueError):
    """The recipe cannot compile — bad parameters or missing capability."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SubjectBehindTextParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    range: SourceRange
    text: str = Field(min_length=1, max_length=120)
    templateId: str = "minimal"
    x: float = Field(default=0.5, ge=0, le=1)
    y: float = Field(default=0.42, ge=0, le=1)
    fontSize: float = Field(default=9.0, gt=0, le=40)
    maskQuality: str = Field(default="faster", pattern="^(faster|better)$")
    animationIn: str = Field(default="fade", pattern="^(none|fade|rise|pop)$")
    animationDuration: float = Field(default=0.35, ge=0.12, le=1.5)
    #: Pull the base plate down a touch so the title reads against it. This is
    #: the recipe's first NON-additive step: it modifies the user's own A-roll
    #: clip, and reverting restores their exact previous grade.
    dimBackground: bool = False


def _base_clip_for_range(
    draft: dict[str, Any] | None, target: SourceRange
) -> tuple[str, SourceRange] | None:
    """The A-roll keep range under the selection, as (clipKey, fingerprint).

    Resolved from the draft at COMPILE time and re-verified against it at
    apply time by the mutation's own fingerprint check. A range without a
    persisted id cannot be addressed durably and returns None — the caller
    degrades with a warning rather than guessing.
    """
    if not isinstance(draft, dict):
        return None
    midpoint = (target.start + target.end) / 2
    for entry in draft.get("keepRanges") or []:
        if not isinstance(entry, dict):
            continue
        range_id = str(entry.get("id") or "").strip()
        try:
            start = float(entry.get("start"))
            end = float(entry.get("end"))
        except (TypeError, ValueError):
            continue
        if not range_id or end <= start:
            continue
        if start - 0.05 <= midpoint <= end + 0.05:
            return f"video:{range_id}", SourceRange(start=start, end=end)
    return None


def compile_subject_behind_text(
    params: dict[str, Any],
    *,
    capability_snapshot: dict[str, Any],
    video_duration: float,
    draft: dict[str, Any] | None = None,
) -> HarnessPlan:
    parsed = SubjectBehindTextParams.model_validate(params)

    if parsed.templateId not in TEXT_TEMPLATES:
        raise CompileError("invalid_template", f"Unknown text template {parsed.templateId!r}.")

    seg = capability(capability_snapshot, "segmentation")
    if not seg.get("available"):
        raise CompileError(
            "capability_unavailable",
            seg.get("reason") or "Background removal is not available on this server.",
        )
    detail = seg.get("detail") or {}
    if not detail.get("autoMatte") and not detail.get("pointPrompt"):
        raise CompileError(
            "capability_unavailable",
            "The segmentation provider supports neither automatic nor prompted removal.",
        )

    max_clip = float((seg.get("limits") or {}).get("maxClipSeconds") or 0)
    span = parsed.range.duration
    if max_clip and span > max_clip:
        raise CompileError(
            "range_too_long",
            f"The selection is {span:.1f}s; background removal here is capped at "
            f"{max_clip:.0f}s. Select a shorter range.",
        )
    if video_duration and parsed.range.end > video_duration + 0.5:
        raise CompileError(
            "range_outside_media",
            "The selection ends after the end of the video.",
        )

    warnings: list[str] = []
    quality = parsed.maskQuality
    if quality == "better" and not detail.get("propagate") and not detail.get("autoMatte"):
        quality = "faster"
        warnings.append("High-quality matte is unavailable here; using the fast matte.")

    operations = [
        DuplicateLinkedOp(
            id="fg",
            range=parsed.range,
            role="foreground",
            explanationKey="harness.op.duplicate_foreground",
        ),
        SubjectMaskOp(
            id="mask",
            targetOp="fg",
            dependsOn=["fg"],
            quality=quality,  # type: ignore[arg-type]
            required=True,
            explanationKey="harness.op.subject_mask",
        ),
        CreateTextOp(
            id="text",
            range=parsed.range,
            text=parsed.text,
            templateId=parsed.templateId,  # type: ignore[arg-type]
            x=parsed.x,
            y=parsed.y,
            fontSize=parsed.fontSize,
            animationIn=parsed.animationIn,  # type: ignore[arg-type]
            animationDuration=parsed.animationDuration,
            explanationKey="harness.op.title_between_layers",
        ),
    ]

    if parsed.dimBackground:
        base = _base_clip_for_range(draft, parsed.range)
        if base is None:
            warnings.append(
                "Could not dim the background: the clip under the selection has "
                "no addressable id yet (save the draft once and re-plan)."
            )
        else:
            clip_key, fingerprint = base
            operations.append(
                AdjustClipOp(
                    id="dim",
                    clipKey=clip_key,
                    fingerprint=fingerprint,
                    # Conservative, on the editor's own −100..100 slider scale:
                    # enough for the title to read, not enough to look graded.
                    settings={"exposure": -22.0, "saturation": -12.0},
                    explanationKey="harness.op.dim_background",
                )
            )

    return HarnessPlan(
        recipe="subject_behind_text",
        recipeVersion=RECIPES["subject_behind_text"]["version"],
        operations=operations,
        warnings=warnings,
    )


class ReviewFixAudio(BaseModel):
    model_config = ConfigDict(extra="forbid")

    volume: float | None = Field(default=None, ge=-60, le=60)
    fadeIn: float | None = Field(default=None, ge=0, le=10)
    fadeOut: float | None = Field(default=None, ge=0, le=10)


class ReviewFixParams(BaseModel):
    """A hardened `fixAction` from an AI-review finding, plus its time range.

    The review job already clamped the numbers (`_harden_fix_action`); this
    model bounds them again because a plan's inputs are validated where they
    are USED, not where they were produced.
    """

    model_config = ConfigDict(extra="forbid")

    range: SourceRange
    adjust: dict[str, float] | None = None
    audio: ReviewFixAudio | None = None
    #: The finding's own words, kept for the plan panel's row label.
    note: str | None = Field(default=None, max_length=200)


def compile_review_fix(
    params: dict[str, Any],
    *,
    capability_snapshot: dict[str, Any],
    video_duration: float,
    draft: dict[str, Any] | None = None,
) -> HarnessPlan:
    parsed = ReviewFixParams.model_validate(params)
    if not parsed.adjust and parsed.audio is None:
        raise CompileError("empty_fix", "This finding carries no applicable fix.")
    if video_duration and parsed.range.start > video_duration + 0.5:
        raise CompileError("range_outside_media", "The finding points past the end of the video.")

    base = _base_clip_for_range(draft, parsed.range)
    if base is None:
        raise CompileError(
            "clip_not_found",
            "The flagged moment has no addressable clip yet — open the editor, "
            "let the draft save once, and try again.",
        )
    clip_key, fingerprint = base

    operations: list[Any] = []
    if parsed.adjust:
        operations.append(
            AdjustClipOp(
                id="fix_adjust",
                clipKey=clip_key,
                fingerprint=fingerprint,
                settings=parsed.adjust,
                explanationKey="harness.op.review_adjust",
            )
        )
    if parsed.audio is not None:
        from app.services.harness.schemas import AudioClipOp

        operations.append(
            AudioClipOp(
                id="fix_audio",
                clipKey=clip_key,
                fingerprint=fingerprint,
                volume=parsed.audio.volume,
                fadeIn=parsed.audio.fadeIn,
                fadeOut=parsed.audio.fadeOut,
                explanationKey="harness.op.review_audio",
            )
        )
    return HarnessPlan(
        recipe="review_fix",
        recipeVersion=RECIPES["review_fix"]["version"],
        operations=operations,
        warnings=[],
    )


_COMPILERS = {
    "subject_behind_text": compile_subject_behind_text,
    "review_fix": compile_review_fix,
}


def compile_recipe(
    recipe_id: str,
    params: dict[str, Any],
    *,
    capability_snapshot: dict[str, Any],
    video_duration: float,
    draft: dict[str, Any] | None = None,
) -> HarnessPlan:
    compiler = _COMPILERS.get(recipe_id)
    if compiler is None:
        raise CompileError("unknown_recipe", f"Unknown recipe {recipe_id!r}.")
    return compiler(
        params,
        capability_snapshot=capability_snapshot,
        video_duration=video_duration,
        draft=draft,
    )


def estimate_plan(plan: HarnessPlan) -> dict[str, Any]:
    """Coarse, honest estimates shown before approval."""
    stage_seconds = 0.0
    affected = 0.0
    for op in plan.operations:
        rng = getattr(op, "range", None)
        if rng is not None:
            affected = max(affected, rng.duration)
        if op.type == "visual.apply_subject_mask":
            # Local segmentation runs roughly realtime-and-a-half on CPU.
            stage_seconds += max(10.0, affected * 1.5)
    return {
        "processingSeconds": round(stage_seconds, 1),
        "providerCostUsd": 0.0,
        "affectedDurationSeconds": round(affected, 3),
        "operationCount": len(plan.operations),
    }


def list_recipes(capability_snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for recipe_id, meta in RECIPES.items():
        missing = [
            key
            for key in meta.get("requires", [])
            if not capability(capability_snapshot, key).get("available")
        ]
        out.append(
            {
                "id": recipe_id,
                "version": meta["version"],
                "title": meta["title"],
                "description": meta["description"],
                "available": not missing,
                **(
                    {
                        "reason": capability(capability_snapshot, missing[0]).get("reason")
                        or f"Requires {', '.join(missing)}."
                    }
                    if missing
                    else {}
                ),
            }
        )
    return out


__all__ = [
    "ANIMATION_PRESETS",
    "CompileError",
    "RECIPES",
    "SubjectBehindTextParams",
    "compile_recipe",
    "estimate_plan",
    "list_recipes",
]
