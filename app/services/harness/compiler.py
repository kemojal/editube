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


def compile_subject_behind_text(
    params: dict[str, Any],
    *,
    capability_snapshot: dict[str, Any],
    video_duration: float,
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
    return HarnessPlan(
        recipe="subject_behind_text",
        recipeVersion=RECIPES["subject_behind_text"]["version"],
        operations=operations,
        warnings=warnings,
    )


_COMPILERS = {
    "subject_behind_text": compile_subject_behind_text,
}


def compile_recipe(
    recipe_id: str,
    params: dict[str, Any],
    *,
    capability_snapshot: dict[str, Any],
    video_duration: float,
) -> HarnessPlan:
    compiler = _COMPILERS.get(recipe_id)
    if compiler is None:
        raise CompileError("unknown_recipe", f"Unknown recipe {recipe_id!r}.")
    return compiler(
        params, capability_snapshot=capability_snapshot, video_duration=video_duration
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
