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


class NormalizedBox(BaseModel):
    """A box in the mask-transform space: centre offsets as percent-of-frame
    (x/y, 0 = frame centre) plus percent width/height — the exact coordinate
    system the tracking backends emit, so no conversion can drift."""

    model_config = ConfigDict(extra="forbid")

    x: float = Field(ge=-50, le=50)
    y: float = Field(ge=-50, le=50)
    width: float = Field(gt=0, le=100)
    height: float = Field(gt=0, le=100)


class TrackObjectOp(_OperationBase):
    """Stage object tracking over a range, seeded from a user-drawn box.

    Phase A work: the propagation backend follows the subject and the staged
    result is `{keyframes: [{t, x, y, width, height}], lostAtSeconds?}` in the
    same transform space the box came in."""

    type: Literal["analysis.track_object"] = "analysis.track_object"
    range: SourceRange
    box: NormalizedBox
    quality: Literal["faster", "better"] = "faster"
    required: bool = True


class StageLabelOp(_OperationBase):
    """Stage a server-rendered label card as a generated image asset."""

    type: Literal["media.stage_label"] = "media.stage_label"
    text: str = Field(min_length=1, max_length=48)
    side: Literal["left", "right"] = "left"
    accent: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")


class PlaceLabelOp(_OperationBase):
    """Put a staged label on the timeline as a first-class image layer.

    A layer rather than a burn-in because burn-ins are static PNGs — a
    tracked callout has to MOVE, and only timeline layers have keyframed
    transforms in both the viewer and the export."""

    type: Literal["timeline.place_label"] = "timeline.place_label"
    labelOp: str = Field(min_length=1, max_length=64)
    range: SourceRange
    widthPct: float = Field(default=18.0, ge=5, le=40)
    trackLabel: str = Field(default="FX", min_length=1, max_length=8)


class TrackKeyframesOp(_OperationBase):
    """Bind a placed layer's position to a staged track, plus an offset.

    Resolved at COMMIT time from the staged tracking result — the keyframes
    cannot be known when the plan is written, only where they come from."""

    type: Literal["motion.track_keyframes"] = "motion.track_keyframes"
    targetOp: str = Field(min_length=1, max_length=64)
    trackOp: str = Field(min_length=1, max_length=64)
    offsetX: float = Field(default=0.0, ge=-60, le=60)
    offsetY: float = Field(default=0.0, ge=-60, le=60)


class MediaAnimation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inPreset: Literal[*ANIMATION_PRESETS] = "none"
    outPreset: Literal[*ANIMATION_PRESETS] = "none"
    duration: float = Field(default=0.35, ge=0.12, le=1.5)
    intensity: int = Field(default=100, ge=10, le=200)


class PlaceMediaOp(_OperationBase):
    """Place an already-generated media asset on the timeline.

    The Director's B-roll placement, as a harness primitive: the asset exists
    before the plan is applied (its generation has its own lifecycle), so
    unlike `media.stage_label` there is no Phase A — this is pure commit.
    """

    type: Literal["timeline.place_media"] = "timeline.place_media"
    name: str = Field(min_length=1, max_length=64)
    kind: Literal["image", "video"] = "image"
    sourceId: int = Field(gt=0)
    sourceUrl: str = Field(min_length=1, max_length=2048)
    assetDuration: float = Field(gt=0, le=3600)
    #: Source-time span on the PRIMARY media the shot covers (the export
    #: intersects it with the keep ranges, so it ripples with later cuts).
    startSec: float = Field(ge=0)
    endSec: float = Field(gt=0)
    #: The intended on-screen length in cut time — the only record of intent
    #: when a removed gap falls inside the source span.
    playDuration: float = Field(gt=0, le=600)
    trackLabel: str = Field(default="V2", min_length=1, max_length=8)
    animation: MediaAnimation | None = None
    keyframes: dict[str, list[Keyframe]] | None = Field(default=None)

    @field_validator("endSec")
    @classmethod
    def _ordered_span(cls, value: float, info: Any) -> float:
        start = info.data.get("startSec")
        if start is not None and value <= float(start):
            raise ValueError("endSec must be after startSec")
        return value

    @field_validator("keyframes")
    @classmethod
    def _exportable_channels(
        cls, value: dict[str, list[Keyframe]] | None
    ) -> dict[str, list[Keyframe]] | None:
        if value is None:
            return None
        if len(value) > 8:
            raise ValueError("too many keyframe channels")
        for channel, track in value.items():
            if not channel.startswith(KEYFRAME_CHANNEL_PREFIXES):
                raise ValueError(
                    f"channel {channel!r} has no server-side export path"
                )
            if not track or len(track) > 200:
                raise ValueError(f"channel {channel!r} keyframe count out of range")
        return value


Operation = Annotated[
    Union[
        DuplicateLinkedOp,
        SubjectMaskOp,
        CreateTextOp,
        ApplyPresetOp,
        SetKeyframesOp,
        AdjustClipOp,
        AudioClipOp,
        TrackObjectOp,
        StageLabelOp,
        PlaceLabelOp,
        TrackKeyframesOp,
        PlaceMediaOp,
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
            for reference in ("targetOp", "labelOp", "trackOp"):
                target = getattr(op, reference, None)
                if target is not None and target not in known:
                    raise ValueError(
                        f"operation {op.id!r} references unknown operation {target!r}"
                    )
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
