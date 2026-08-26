"""Putting the planned shots onto the timeline.

This is where director time becomes source time, and where a plan stops being a
document and becomes an edit. Three things make it more delicate than it looks.

**The clocks differ.** The model reasoned about the cut; `timelineMediaItems`
store seconds in the *original file*. A shot placed with the wrong conversion
lands on the wrong sentence, and the failure is silent — a B-roll clip in the
wrong place looks exactly like a B-roll clip. See §6.

**A shot can span a cut.** Its source range then covers footage the user
removed. That is correct and deliberate: the export intersects layers with
`keepRanges` and splits them, so the shot ripples with the cut instead of
drifting off it. What must be preserved separately is how long the shot was
*meant* to be on screen, which is why `playDuration` is set from the
director-time span rather than the source span.

**The output has to be reproducible.** The backend compiles the draft and the
editor replays the same plan visually on top of the pre-director snapshot
(§4.2). If the two disagree the user watches one edit happen and then the
autosave writes a different one, so every id and ordering decision here is
derived from the plan rather than from wall-clock or insertion order.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.services import director_manifest as manifest
from app.services.director_context import DirectorContext, resolve_anchor

logger = logging.getLogger(__name__)

#: Ken Burns is expressed on `video.scale`, which the editor and the render both
#: read as a *percentage* with 100 as neutral — not a multiplier.
SCALE_NEUTRAL = 100.0

#: Below this the move is not visible; above it, it reads as a lurch rather than
#: a drift. The prompt asks for 4–8%; this is the hard bound behind it.
MAX_KEN_BURNS_DELTA = 0.4


@dataclass
class CompiledPlan:
    """A draft ready to save, plus exactly what was added to it."""

    draft: dict[str, Any]
    manifest: dict[str, list[str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    placed: int = 0


def _track_stack(tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise track ordering the way the editor does.

    Mirrors `normalizeTimelineTracks`: video and text share one ordered visual
    stack (order 0 is topmost), audio always sits below it, and the final orders
    are sequential indices rather than whatever numbers happened to be stored.
    """
    ordered = sorted(
        tracks,
        key=lambda track: (
            1 if track.get("kind") == "audio" else 0,
            int(track.get("order", 0)),
        ),
    )
    return [{**track, "order": index} for index, track in enumerate(ordered)]


def ensure_broll_track(tracks: list[dict[str, Any]], label: str) -> tuple[list[dict[str, Any]], str]:
    """Find or create the B-roll track, positioned above V1 but below the text.

    Above V1 because that is what B-roll means; below the text because a title
    the shot covers is a title nobody reads. Returns the tracks and the id.
    """
    for track in tracks:
        if track.get("kind") == "video" and track.get("label") == label:
            return _track_stack(tracks), str(track["id"])

    video_tracks = [t for t in tracks if t.get("kind") == "video"]
    base_order = min((int(t.get("order", 0)) for t in video_tracks), default=1)
    new_track = {
        "id": f"track-video-{len(video_tracks) + 1}",
        "kind": "video",
        "label": label,
        "enabled": True,
        "locked": False,
        "height": 44,
        # Immediately above the topmost existing video track. The `- 0.5` is
        # only a sort key: `_track_stack` renumbers everything to sequential
        # indices straight after.
        "order": base_order - 0.5,
    }
    return _track_stack([*tracks, new_track]), str(new_track["id"])


def _ken_burns_keyframes(framing: Any, duration: float) -> dict[str, list[dict[str, Any]]]:
    """A Ken Burns move as a keyframed scale ramp.

    Keyframes rather than a looping `combo` animation: the editor offers both,
    but keyframes are what the inspector can show and the user can adjust, and
    offering the model two routes to one result is how thirty clips stop looking
    like one hand (see the manifest's second filter).
    """
    if not isinstance(framing, dict):
        return {}
    ken_burns = framing.get("kenBurns")
    if not isinstance(ken_burns, dict):
        return {}
    try:
        start = float(ken_burns.get("from", 1.0))
        end = float(ken_burns.get("to", 1.0))
    except (TypeError, ValueError):
        return {}

    easing = str(ken_burns.get("easing") or "smooth")
    if easing not in manifest.EASING_CURVES:
        easing = "smooth"
    # A move bigger than this stops reading as a drift. Clamp rather than drop:
    # the intent was right even when the number was not.
    end = start + max(-MAX_KEN_BURNS_DELTA, min(MAX_KEN_BURNS_DELTA, end - start))
    if abs(end - start) < 0.005:
        return {}

    return {
        "video.scale": [
            {"t": 0.0, "v": round(start * SCALE_NEUTRAL, 3), "easing": easing},
            {"t": round(duration, 3), "v": round(end * SCALE_NEUTRAL, 3)},
        ]
    }


def _animation(raw: Any, key: str) -> tuple[str, float]:
    if not isinstance(raw, dict):
        return "none", 0.35
    preset = str(raw.get(key) or raw.get("preset") or "none")
    if preset not in manifest.ANIMATION_PRESETS:
        preset = "none"
    try:
        duration = float(raw.get("durationSeconds", 0.35))
    except (TypeError, ValueError):
        duration = 0.35
    return preset, max(0.12, min(1.5, duration))


def _clip_name(directive: dict[str, Any]) -> str:
    """What the clip is called on the timeline.

    The shot description, not the directive id: the user is scanning a timeline
    and "cluttered desk at dusk" tells them what the clip is, while `d7` does
    not.
    """
    asset = directive.get("asset") if isinstance(directive.get("asset"), dict) else {}
    text = str(asset.get("prompt") or "").strip() or "B-roll"
    return text if len(text) <= 48 else f"{text[:47]}…"


def compile_plan(
    draft: dict[str, Any],
    plan: dict[str, Any],
    *,
    context: DirectorContext,
    assets_by_directive: dict[str, Any],
    plan_id: int,
    applied_at: str,
) -> CompiledPlan:
    """Place every shot with a finished asset onto the draft.

    A directive whose asset failed or is still generating is skipped with a
    warning, never placed empty — an empty clip on the timeline is worse than
    an uninterrupted stretch of the speaker's face, because the user has to find
    it and remove it.
    """
    result = CompiledPlan(draft=dict(draft))
    tracks = list(result.draft.get("timelineTracks") or [])
    if not tracks:
        # An untouched draft has no explicit track list yet; the editor's own
        # defaults are what it would have created on load.
        tracks = [
            {"id": "track-text-1", "kind": "text", "label": "TX1", "enabled": True, "locked": False, "height": 22, "order": 0},
            {"id": "track-video-1", "kind": "video", "label": "V1", "enabled": True, "locked": False, "height": 44, "order": 1},
            {"id": "track-audio-1", "kind": "audio", "label": "A1", "enabled": True, "muted": False, "locked": False, "height": 34, "order": 2},
        ]

    tracks, broll_track_id = ensure_broll_track(tracks, "V2")

    items = list(result.draft.get("timelineMediaItems") or [])
    attributes = dict(result.draft.get("clipAttributes") or {})
    existing_ids = {str(item.get("id")) for item in items}

    created_items: list[str] = []
    created_keys: list[str] = []

    for directive in plan.get("directives") or []:
        if not isinstance(directive, dict):
            continue
        directive_id = str(directive.get("id") or "")
        asset = assets_by_directive.get(directive_id)
        if asset is None or getattr(asset, "status", "") != "ready" or not getattr(asset, "url", ""):
            result.warnings.append(
                f"No usable shot for {_clip_name(directive)!r}; left the A-roll running."
            )
            continue

        # Ids are derived from the plan, not minted: re-applying the same plan
        # must be a no-op rather than a second copy of every clip.
        item_id = f"dir{plan_id}-{directive_id}"
        if item_id in existing_ids:
            continue

        anchor = directive.get("anchor") if isinstance(directive.get("anchor"), dict) else {}
        try:
            director_start = float(directive.get("start", 0.0))
            director_end = float(directive.get("end", 0.0))
        except (TypeError, ValueError):
            result.warnings.append(f"Unreadable timing on {_clip_name(directive)!r}; skipped.")
            continue
        span = max(0.1, director_end - director_start)

        resolved = resolve_anchor(
            context,
            segment_id=str(anchor.get("segmentId") or ""),
            quote=str(anchor.get("quote") or ""),
            fallback_director_start=director_start,
            fallback_director_end=director_end,
        )
        if not resolved.exact:
            # Two very different situations, and the user can act on one of
            # them: footage they cut can be brought back, words nobody said
            # cannot. Saying "could not find" for both would send them looking
            # through a transcript for a line that is right there.
            quote = str(anchor.get("quote") or "")
            if context.was_cut(quote):
                result.warnings.append(
                    f"{_clip_name(directive)!r} was anchored to a line that has since been cut; skipped."
                )
            else:
                result.warnings.append(
                    f"Could not find the words {_clip_name(directive)!r} was anchored to; skipped."
                )
            continue

        # Source time, via the cut. The shot starts on the anchored word and
        # runs for the length the director asked for *in the cut* — so if a
        # removed gap falls inside it, the source range spans the gap and the
        # export's own intersection splits the clip around it.
        source_start = resolved.start
        anchor_in_cut = context.cut_map.to_director(source_start)
        if anchor_in_cut is None:
            # Defensive: the anchor resolved against surviving words, so it is
            # inside a kept range by construction.
            result.warnings.append(
                f"{_clip_name(directive)!r} anchors to footage that was cut; skipped."
            )
            continue
        source_end = context.cut_map.to_source(anchor_in_cut + span)
        if source_end <= source_start:
            source_end = source_start + span

        kind = "video" if getattr(asset, "kind", "image") == "video" else "image"
        in_preset, in_duration = _animation(directive.get("animationIn"), "preset")
        out_preset, out_duration = _animation(directive.get("animationOut"), "preset")

        items.append(
            {
                "id": item_id,
                "trackId": broll_track_id,
                "track": "video",
                "mediaKey": f"generated-{asset.id}",
                # The discriminator that stops the render resolving this to the
                # primary video and compositing the A-roll in its place (G2).
                "sourceKind": "generated",
                "sourceId": int(asset.id),
                "name": _clip_name(directive),
                "kind": kind,
                "sourceUrl": str(asset.url),
                "duration": float(getattr(asset, "duration_seconds", None) or span),
                "start": round(source_start, 3),
                "end": round(source_end, 3),
                "sourceStart": 0.0,
                # How long the shot is *meant* to be on screen. The source span
                # above can be longer when a cut falls inside it, so this is the
                # only record of the director's actual intent.
                "playDuration": round(span, 3),
                # B-roll ships muted: the A-roll is still the programme.
                "audioEnabled": False,
            }
        )
        existing_ids.add(item_id)
        created_items.append(item_id)

        clip_key = f"media:{item_id}"
        clip_attrs: dict[str, Any] = {}
        if in_preset != "none" or out_preset != "none":
            clip_attrs["animation"] = {
                "inPreset": in_preset,
                "outPreset": out_preset,
                "duration": round(max(in_duration, out_duration), 3),
                "intensity": 100,
            }
        keyframes = _ken_burns_keyframes(directive.get("framing"), span)
        if keyframes:
            clip_attrs["keyframes"] = keyframes
        if clip_attrs:
            attributes[clip_key] = clip_attrs
            created_keys.append(clip_key)

        result.placed += 1

    result.draft["timelineTracks"] = tracks
    result.draft["timelineMediaItems"] = items
    result.draft["clipAttributes"] = attributes
    result.draft["directorPlanId"] = plan_id
    result.draft["directorAppliedAt"] = applied_at
    result.manifest = {
        "timelineMediaItemIds": created_items,
        "clipAttributeKeys": created_keys,
        "trackIds": [broll_track_id],
    }

    logger.info(
        "Compiled director plan %s: %s shot(s) placed, %s skipped",
        plan_id,
        result.placed,
        len(result.warnings),
    )
    return result


def revert_plan(draft: dict[str, Any], applied: dict[str, Any]) -> dict[str, Any]:
    """Remove everything a run added, using the manifest it recorded.

    A filter over recorded ids rather than a diff: the user has very likely
    edited since, and a diff would either undo their work or refuse to run. The
    B-roll track itself is only removed if nothing else ended up on it — the
    user may have put their own clips there.
    """
    result = dict(draft)
    item_ids = set(applied.get("timelineMediaItemIds") or [])
    attribute_keys = set(applied.get("clipAttributeKeys") or [])
    track_ids = set(applied.get("trackIds") or [])

    items = [
        item
        for item in (result.get("timelineMediaItems") or [])
        if str(item.get("id")) not in item_ids
    ]
    attributes = {
        key: value
        for key, value in (result.get("clipAttributes") or {}).items()
        if key not in attribute_keys
    }

    occupied = {str(item.get("trackId")) for item in items}
    tracks = [
        track
        for track in (result.get("timelineTracks") or [])
        if str(track.get("id")) not in track_ids or str(track.get("id")) in occupied
    ]

    result["timelineMediaItems"] = items
    result["clipAttributes"] = attributes
    result["timelineTracks"] = _track_stack(tracks)
    result.pop("directorPlanId", None)
    result.pop("directorAppliedAt", None)
    return result
