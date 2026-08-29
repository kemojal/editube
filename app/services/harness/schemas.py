"""Versioned operation and plan contracts for the editing harness.

Every mutation the harness can make is one of these models. The rules from
docs/editing-harness-implementation-plan.md §10:

- `extra="forbid"` everywhere — an unknown field is a rejected plan, never a
  silently ignored one.
- Bounded numerics; allow-listed enums generated from the same constants the
  execution engine uses, so the validator and the renderer cannot drift.
- Entity ids are never minted by callers: they derive from
  `(run_id, operation id)` via `entity_id`, which is what makes re-apply a
  structural no-op.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 1

try:  # The Director's easing allow-list is the renderer's ground truth.
    from app.services import director_manifest as _manifest

    EASING_CURVES: tuple[str, ...] = tuple(_manifest.EASING_CURVES)
except Exception:  # noqa: BLE001 — keep the harness importable without the Director
    EASING_CURVES = (
        "linear", "hold", "ease-in", "ease-out", "ease-in-out",
        "smooth", "glide", "snappy", "anticipate", "settle", "overshoot",
    )

#: Presets the exporter actually implements (`_animation_presets`,
#: rough_cut_export.py:1547). `slide-down` is deliberately absent — it does
#: not exist server-side.
ANIMATION_PRESETS = (
    "none", "fade", "zoom", "pop", "slide-left", "slide-right", "slide-up",
    "spin", "swing", "shake", "pulse", "focus",
)

#: Text overlay templates the editor ships (TextOverlayStyle.templateId).
TEXT_TEMPLATES = ("editorial", "glass", "minimal", "broadcast", "mono", "captionbar", "corner")

#: Keyframe channels with a server-side export expression. Only `video.*` and
#: `adjust.*` prefixes exist in the renderer.
KEYFRAME_CHANNEL_PREFIXES = ("video.", "adjust.")

SEMANTIC_ROLES = ("base", "foreground", "background", "overlay", "matte", "audio", "support")


def entity_id(run_id: int, op_id: str) -> str:
    """Deterministic id for anything an operation creates."""
    return f"ehr{run_id}-{op_id}"


def group_id(run_id: int) -> str:
    return f"grp-ehr{run_id}"


class SourceRange(BaseModel):
    """A source-time interval, seconds. `timeBasis` is explicit by contract."""

    model_config = ConfigDict(extra="forbid")

    timeBasis: Literal["source"] = "source"
    start: float = Field(ge=0)
    end: float = Field(gt=0)

    @field_validator("end")
    @classmethod
    def _ordered(cls, value: float, info: Any) -> float:
        start = info.data.get("start")
        if start is not None and value <= float(start):
            raise ValueError("range end must be after start")
        return value

    @property
    def duration(self) -> float:
        return self.end - self.start


class _OperationBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    schemaVersion: int = SCHEMA_VERSION
    dependsOn: list[str] = Field(default_factory=list, max_length=16)
    enabled: bool = True
    risk: Literal["non_destructive", "reversible", "destructive"] = "reversible"
    #: Rendered by trusted UI templates; never model prose.
    explanationKey: str | None = None


class DuplicateLinkedOp(_OperationBase):
    """A visual-only linked duplicate of the primary media over a source range.

    The duplicate references the same source (`sourceKind: "video"` +
    `sourceId`); it never copies the media object, and it never owns audio.
    """

    type: Literal["timeline.duplicate_linked"] = "timeline.duplicate_linked"
    range: SourceRange
    role: Literal["foreground", "background", "support"] = "foreground"
    trackLabel: str = Field(default="FX", min_length=1, max_length=8)


class SubjectMaskOp(_OperationBase):
    """Stage background removal for an entity created earlier in this run."""

    type: Literal["visual.apply_subject_mask"] = "visual.apply_subject_mask"
    targetOp: str = Field(min_length=1, max_length=64)
    quality: Literal["faster", "better"] = "faster"
    #: Redaction-intent masks fail closed at export (plan §3.1).
    required: bool = True


class CreateTextOp(_OperationBase):
    """A persisted text overlay (draft key `textOverlays`)."""

    type: Literal["overlay.create_text"] = "overlay.create_text"
    text: str = Field(min_length=1, max_length=200)
    range: SourceRange
    templateId: Literal[*TEXT_TEMPLATES] = "minimal"
    x: float = Field(default=0.5, ge=0, le=1)
    y: float = Field(default=0.42, ge=0, le=1)
    fontSize: float = Field(default=9.0, gt=0, le=40)
    animationIn: Literal["none", "fade", "rise", "pop"] = "fade"
    animationOut: Literal["none", "fade"] = "fade"
    animationDuration: float = Field(default=0.35, ge=0.12, le=1.5)


class ApplyPresetOp(_OperationBase):
    type: Literal["motion.apply_preset"] = "motion.apply_preset"
    targetOp: str = Field(min_length=1, max_length=64)
    mode: Literal["in", "out", "combo"] = "combo"
    preset: Literal[*ANIMATION_PRESETS] = "fade"
    duration: float = Field(default=0.35, ge=0.12, le=1.5)
    intensity: int = Field(default=100, ge=10, le=200)


class Keyframe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    t: float = Field(ge=0)
    v: float
    easing: str | None = None

    @field_validator("easing")
    @classmethod
    def _known_easing(cls, value: str | None) -> str | None:
        if value is not None and value not in EASING_CURVES:
            raise ValueError(f"unknown easing {value!r}")
        return value


class SetKeyframesOp(_OperationBase):
    type: Literal["motion.set_keyframes"] = "motion.set_keyframes"
    targetOp: str = Field(min_length=1, max_length=64)
    channel: str = Field(min_length=1, max_length=64)
    keyframes: list[Keyframe] = Field(min_length=1, max_length=200)

    @field_validator("channel")
    @classmethod
    def _exportable_channel(cls, value: str) -> str:
        if not value.startswith(KEYFRAME_CHANNEL_PREFIXES):
            raise ValueError(
                "only video.* and adjust.* channels have a server-side export path"
            )
        return value


#: Adjust keys a harness plan may set — the subset of the editor's grade with
#: unambiguous, bounded semantics. Everything is the editor's own −100..100
#: slider scale (`app/services/color_adjust.py` clamps identically).
ADJUSTABLE_KEYS = (
    "temp", "tint", "exposure", "contrast", "saturation", "vibrance",
    "highlight", "shadow", "brilliance", "fade",
)


class _ExistingClipOp(_OperationBase):
    """An operation on a clip the USER made — the non-additive family.

    `clipKey` addresses the clip the way the editor does
    (`<track>:<rangeId>` / `media:<itemId>`); `fingerprint` records the
    source range the plan believed that key covered. At apply time a missing
    key or a moved fingerprint SKIPS the operation with a warning — the
    anchor-miss discipline: never modify approximately.
    """

    clipKey: str = Field(min_length=1, max_length=128, pattern=r"^(video|audio|media):")
    fingerprint: SourceRange | None = None


class AdjustClipOp(_ExistingClipOp):
    type: Literal["visual.adjust"] = "visual.adjust"
    settings: dict[str, float] = Field(min_length=1, max_length=len(ADJUSTABLE_KEYS))

    @field_validator("settings")
    @classmethod
    def _bounded_known_keys(cls, value: dict[str, float]) -> dict[str, float]:
        for key, amount in value.items():
            if key not in ADJUSTABLE_KEYS:
                raise ValueError(f"unknown adjust key {key!r}")
            if not -100 <= float(amount) <= 100:
                raise ValueError(f"adjust {key} out of the -100..100 slider range")
        return value


class AudioClipOp(_ExistingClipOp):
    type: Literal["audio.adjust"] = "audio.adjust"
    volume: float | None = Field(default=None, ge=-60, le=60)
    fadeIn: float | None = Field(default=None, ge=0, le=10)
    fadeOut: float | None = Field(default=None, ge=0, le=10)

    @model_validator(mode="after")
    def _something_to_do(self) -> "AudioClipOp":
        if self.volume is None and self.fadeIn is None and self.fadeOut is None:
            raise ValueError("audio.adjust needs at least one of volume/fadeIn/fadeOut")
        return self


Operation = Annotated[
    Union[
        DuplicateLinkedOp,
        SubjectMaskOp,
        CreateTextOp,
        ApplyPresetOp,
        SetKeyframesOp,
        AdjustClipOp,
        AudioClipOp,
    ],
    Field(discriminator="type"),
]


class HarnessPlan(BaseModel):
    """The compiled, primitive-only plan the user reviews and the engine runs."""

    model_config = ConfigDict(extra="forbid")

    schemaVersion: int = SCHEMA_VERSION
    recipe: str
    recipeVersion: int = 1
    operations: list[Operation] = Field(min_length=1, max_length=200)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("operations")
    @classmethod
    def _valid_graph(cls, ops: list[Any]) -> list[Any]:
        ids = [op.id for op in ops]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate operation ids")
        known = set(ids)
        for op in ops:
            for dep in op.dependsOn:
                if dep not in known:
                    raise ValueError(f"operation {op.id!r} depends on unknown {dep!r}")
                if dep == op.id:
                    raise ValueError(f"operation {op.id!r} depends on itself")
            target = getattr(op, "targetOp", None)
            if target is not None and target not in known:
                raise ValueError(f"operation {op.id!r} targets unknown operation {target!r}")
        # Cycle check over dependsOn (Kahn).
        remaining = {op.id: set(op.dependsOn) for op in ops}
        while remaining:
            free = [op_id for op_id, deps in remaining.items() if not deps]
            if not free:
                raise ValueError("operation dependency cycle")
            for op_id in free:
                remaining.pop(op_id)
            for deps in remaining.values():
                deps.difference_update(free)
        return ops


def plan_checksum(plan: HarnessPlan) -> str:
    canonical = json.dumps(plan.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
