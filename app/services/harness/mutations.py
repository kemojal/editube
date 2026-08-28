"""Pure draft mutations for harness operations, with inverse manifests.

Dict in, dict out — no database, no network, no clock. The same functions run
in three places: the diff simulator, the commit path, and (eventually) the
frontend replay, which is why determinism is a hard requirement and why every
entity id is derived rather than minted (the Director's compile discipline,
generalized).

Unlike the Director's additive-only manifest, every mutation records inverse
entries rich enough to revert non-additive edits:

- ``{"op": "remove_timeline_item", "id": ...}``
- ``{"op": "remove_track_if_unused", "id": ...}``
- ``{"op": "remove_text_overlay", "id": ...}``
- ``{"op": "remove_clip_attribute_key", "key": ...}``
- ``{"op": "restore_value", "path": [...], "before": <json|absent>, "after": <json>}``

`restore_value` carries both sides so a revert can detect that the location
changed since apply and refuse that entry instead of clobbering newer work.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from app.services.harness.schemas import (
    ApplyPresetOp,
    CreateTextOp,
    DuplicateLinkedOp,
    HarnessPlan,
    SetKeyframesOp,
    SubjectMaskOp,
    entity_id,
    group_id,
)

_ABSENT = {"__absent__": True}


@dataclass
class MutationContext:
    run_id: int
    video_id: int
    video_duration: float
    source_url: str | None = None
    #: op id -> staged asset dict (e.g. {"resultId": ..., "outputUrl": ...}).
    staged_assets: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class MutationResult:
    draft: dict[str, Any]
    created_item_ids: list[str] = field(default_factory=list)
    created_overlay_ids: list[str] = field(default_factory=list)
    created_track_ids: list[str] = field(default_factory=list)
    created_attribute_keys: list[str] = field(default_factory=list)
    inverse: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def manifest(self) -> dict[str, Any]:
        return {
            "timelineMediaItemIds": list(self.created_item_ids),
            "textOverlayIds": list(self.created_overlay_ids),
            "trackIds": list(self.created_track_ids),
            "clipAttributeKeys": list(self.created_attribute_keys),
        }


def _track_stack(tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mirror of the editor's `normalizeTimelineTracks` (and the Director's copy)."""
    ordered = sorted(
        tracks,
        key=lambda track: (
            1 if track.get("kind") == "audio" else 0,
            float(track.get("order", 0)),
        ),
    )
    return [{**track, "order": index} for index, track in enumerate(ordered)]


def _ensure_top_video_track(
    draft: dict[str, Any], label: str, run_id: int, result: MutationResult
) -> str:
    """Find or create a video track above every existing video track.

    Above V1 because the foreground must cover the base; the text layer's
    position relative to it is decided by the export's above/below-text split,
    which follows track order.
    """
    tracks = [dict(t) for t in (draft.get("timelineTracks") or []) if isinstance(t, dict)]
    if not tracks:
        tracks = [
            {"id": "track-text-1", "kind": "text", "label": "TX1", "enabled": True,
             "locked": False, "height": 22, "order": 0},
            {"id": "track-video-1", "kind": "video", "label": "V1", "enabled": True,
             "locked": False, "height": 44, "order": 1},
            {"id": "track-audio-1", "kind": "audio", "label": "A1", "enabled": True,
             "muted": False, "locked": False, "height": 34, "order": 2},
        ]

    for track in tracks:
        if track.get("kind") == "video" and track.get("label") == label:
            draft["timelineTracks"] = _track_stack(tracks)
            return str(track["id"])

    video_tracks = [t for t in tracks if t.get("kind") == "video"]
    base_order = min((float(t.get("order", 0)) for t in video_tracks), default=1.0)
    new_track = {
        "id": f"track-video-ehr{run_id}",
        "kind": "video",
        "label": label,
        "enabled": True,
        "locked": False,
        "height": 44,
        "order": base_order - 0.5,
    }
    draft["timelineTracks"] = _track_stack([*tracks, new_track])
    result.created_track_ids.append(new_track["id"])
    result.inverse.append({"op": "remove_track_if_unused", "id": new_track["id"]})
    return new_track["id"]


def _set_attribute(
    draft: dict[str, Any],
    clip_key: str,
    name: str,
    value: Any,
    result: MutationResult,
) -> None:
    attrs_map = dict(draft.get("clipAttributes") or {})
    attrs = dict(attrs_map.get(clip_key) or {})
    is_new_key = clip_key not in attrs_map
    before = deepcopy(attrs[name]) if name in attrs else _ABSENT
    attrs[name] = deepcopy(value)
    attrs_map[clip_key] = attrs
    draft["clipAttributes"] = attrs_map
    if is_new_key:
        if clip_key not in result.created_attribute_keys:
            result.created_attribute_keys.append(clip_key)
            result.inverse.append({"op": "remove_clip_attribute_key", "key": clip_key})
    else:
        result.inverse.append(
            {
                "op": "restore_value",
                "path": ["clipAttributes", clip_key, name],
                "before": before,
                "after": deepcopy(value),
            }
        )


def _apply_duplicate(
    draft: dict[str, Any], op: DuplicateLinkedOp, ctx: MutationContext, result: MutationResult
) -> None:
    item_id = entity_id(ctx.run_id, op.id)
    items = [dict(i) for i in (draft.get("timelineMediaItems") or []) if isinstance(i, dict)]
    if any(str(i.get("id")) == item_id for i in items):
        # Re-apply is a structural no-op (derived ids).
        draft["timelineMediaItems"] = items
        return
    track_id = _ensure_top_video_track(draft, op.trackLabel, ctx.run_id, result)
    span = op.range.duration
    item = {
        "id": item_id,
        "trackId": track_id,
        "track": "video",
        "mediaKey": f"video-{ctx.video_id}",
        "sourceKind": "video",
        "sourceId": int(ctx.video_id),
        "name": "Foreground subject" if op.role == "foreground" else "Linked duplicate",
        "kind": "video",
        "sourceUrl": ctx.source_url or "",
        "duration": round(float(ctx.video_duration or span), 3),
        "start": round(op.range.start, 3),
        "end": round(op.range.end, 3),
        "sourceStart": round(op.range.start, 3),
        "playDuration": round(span, 3),
        # A linked duplicate never owns audio — the base keeps it (plan §7.1).
        "audioEnabled": False,
        "groupId": group_id(ctx.run_id),
        "linkId": f"link-ehr{ctx.run_id}-{op.id}",
        "semanticRole": op.role,
        "createdByRunId": ctx.run_id,
    }
    items.append(item)
    draft["timelineMediaItems"] = items
    result.created_item_ids.append(item_id)
    result.inverse.append({"op": "remove_timeline_item", "id": item_id})


def _apply_subject_mask(
    draft: dict[str, Any], op: SubjectMaskOp, ctx: MutationContext, result: MutationResult
) -> None:
    target_id = entity_id(ctx.run_id, op.targetOp)
    clip_key = f"media:{target_id}"
    staged = ctx.staged_assets.get(op.id) or {}
    _set_attribute(
        draft,
        clip_key,
        "removeBg",
        {"autoRemoval": True, "quality": op.quality},
        result,
    )
    processing = {
        "remove_bg": {
            "resultId": staged.get("resultId"),
            "status": "completed" if staged.get("outputUrl") else "queued",
            "progress": 100 if staged.get("outputUrl") else 0,
            **({"outputUrl": staged["outputUrl"]} if staged.get("outputUrl") else {}),
        }
    }
    _set_attribute(draft, clip_key, "processing", processing, result)


def _apply_create_text(
    draft: dict[str, Any], op: CreateTextOp, ctx: MutationContext, result: MutationResult
) -> None:
    overlay_id = entity_id(ctx.run_id, op.id)
    overlays = [dict(o) for o in (draft.get("textOverlays") or []) if isinstance(o, dict)]
    if any(str(o.get("id")) == overlay_id for o in overlays):
        draft["textOverlays"] = overlays
        return
    overlay = {
        "id": overlay_id,
        "kind": "block",
        "templateId": op.templateId,
        "text": op.text,
        "start": round(op.range.start, 3),
        "end": round(op.range.end, 3),
        "x": round(op.x, 4),
        "y": round(op.y, 4),
        "fontSize": round(op.fontSize, 2),
        "animationIn": op.animationIn,
        "animationOut": op.animationOut,
        "animationDuration": round(op.animationDuration, 3),
        "groupId": group_id(ctx.run_id),
        "semanticRole": "overlay",
        "createdByRunId": ctx.run_id,
    }
    overlays.append(overlay)
    draft["textOverlays"] = overlays
    result.created_overlay_ids.append(overlay_id)
    result.inverse.append({"op": "remove_text_overlay", "id": overlay_id})


def _apply_preset(
    draft: dict[str, Any], op: ApplyPresetOp, ctx: MutationContext, result: MutationResult
) -> None:
    target_id = entity_id(ctx.run_id, op.targetOp)
    _set_attribute(
        draft,
        f"media:{target_id}",
        "animation",
        {
            "mode": op.mode,
            "preset": op.preset,
            "duration": round(op.duration, 3),
            "intensity": int(op.intensity),
        },
        result,
    )


def _apply_keyframes(
    draft: dict[str, Any], op: SetKeyframesOp, ctx: MutationContext, result: MutationResult
) -> None:
    target_id = entity_id(ctx.run_id, op.targetOp)
    clip_key = f"media:{target_id}"
    attrs_map = dict(draft.get("clipAttributes") or {})
    attrs = dict(attrs_map.get(clip_key) or {})
    keyframes = dict(attrs.get("keyframes") or {})
    keyframes[op.channel] = [
        {"t": round(kf.t, 3), "v": kf.v, **({"easing": kf.easing} if kf.easing else {})}
        for kf in op.keyframes
    ]
    _set_attribute(draft, clip_key, "keyframes", keyframes, result)


_APPLIERS = {
    "timeline.duplicate_linked": _apply_duplicate,
    "visual.apply_subject_mask": _apply_subject_mask,
    "overlay.create_text": _apply_create_text,
    "motion.apply_preset": _apply_preset,
    "motion.set_keyframes": _apply_keyframes,
}


def apply_plan(
    draft: dict[str, Any], plan: HarnessPlan, ctx: MutationContext
) -> MutationResult:
    """Apply every enabled operation, in dependency order, purely."""
    result = MutationResult(draft=deepcopy(draft))
    enabled = {op.id for op in plan.operations if op.enabled}
    for op in plan.operations:
        if not op.enabled:
            result.warnings.append(f"Operation {op.id} is disabled; skipped.")
            continue
        missing = [dep for dep in op.dependsOn if dep not in enabled]
        if missing:
            result.warnings.append(
                f"Operation {op.id} skipped: depends on disabled {', '.join(missing)}."
            )
            enabled.discard(op.id)
            continue
        applier = _APPLIERS.get(op.type)
        if applier is None:  # pragma: no cover — schema forbids unknown types
            result.warnings.append(f"Operation {op.id} has no applier; skipped.")
            continue
        applier(result.draft, op, ctx, result)
    return result


def _values_equal(a: Any, b: Any) -> bool:
    return a == b


def revert_manifest(
    draft: dict[str, Any], inverse: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[str]]:
    """Replay inverse entries in reverse order. Returns (draft, warnings).

    A `restore_value` whose location no longer holds the recorded `after`
    value is refused with a warning rather than clobbering newer work.
    """
    result = deepcopy(draft)
    warnings: list[str] = []
    for entry in reversed(inverse or []):
        op = entry.get("op")
        if op == "remove_timeline_item":
            items = [i for i in (result.get("timelineMediaItems") or []) if isinstance(i, dict)]
            result["timelineMediaItems"] = [
                i for i in items if str(i.get("id")) != str(entry.get("id"))
            ]
        elif op == "remove_text_overlay":
            overlays = [o for o in (result.get("textOverlays") or []) if isinstance(o, dict)]
            remaining = [o for o in overlays if str(o.get("id")) != str(entry.get("id"))]
            if remaining:
                result["textOverlays"] = remaining
            else:
                # An empty list is indistinguishable from an absent key to the
                # loader; dropping it keeps apply∘revert an exact identity.
                result.pop("textOverlays", None)
        elif op == "remove_clip_attribute_key":
            attrs = dict(result.get("clipAttributes") or {})
            attrs.pop(str(entry.get("key")), None)
            result["clipAttributes"] = attrs
        elif op == "remove_track_if_unused":
            track_id = str(entry.get("id"))
            occupied = {
                str(i.get("trackId"))
                for i in (result.get("timelineMediaItems") or [])
                if isinstance(i, dict)
            }
            if track_id in occupied:
                warnings.append(
                    f"Track {track_id} kept: other clips now live on it."
                )
            else:
                remaining_tracks = [
                    dict(t)
                    for t in (result.get("timelineTracks") or [])
                    if isinstance(t, dict) and str(t.get("id")) != track_id
                ]
                # Re-normalise orders: creating the track renumbered the whole
                # stack, so removing it must too, or revert is not an identity.
                result["timelineTracks"] = _track_stack(remaining_tracks)
        elif op == "restore_value":
            path = entry.get("path") or []
            if not path:
                continue
            node: Any = result
            ok = True
            for key in path[:-1]:
                if not isinstance(node, dict) or key not in node:
                    ok = False
                    break
                node = node[key]
            leaf = path[-1]
            if not ok or not isinstance(node, dict):
                warnings.append(f"Could not restore {'/'.join(map(str, path))}: path is gone.")
                continue
            current = node.get(leaf, _ABSENT)
            if not _values_equal(current, entry.get("after")):
                warnings.append(
                    f"Left {'/'.join(map(str, path))} alone: it was changed after this run."
                )
                continue
            before = entry.get("before", _ABSENT)
            if before == _ABSENT:
                node.pop(leaf, None)
            else:
                node[leaf] = deepcopy(before)
        else:
            warnings.append(f"Unknown inverse entry {op!r}; skipped.")
    return result, warnings
