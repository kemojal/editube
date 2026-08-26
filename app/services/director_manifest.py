"""What the creative director is allowed to ask for.

This is the single place that answers "can the editor actually do this?", and it
is deliberately *narrower* than what the editor can do. Three filters apply, in
order:

1. **It must render.** Anything that shows in the editor preview but not in the
   exported MP4 is excluded, however good it looks on screen. A director that
   plans titles the render drops produces an export that does not match what the
   user approved, which is worse than not planning them at all.
2. **It must be one way of doing a thing.** Where the editor offers two routes
   to the same result (a looping `combo` animation vs. keyframed Ken Burns), the
   manifest offers one. Two routes means the model picks arbitrarily and thirty
   clips stop looking like one hand.
3. **It must be worth choosing between.** Eleven easing curves is a menu; seven
   is a vocabulary. Curation is part of the craft bar, not a limitation of it.

The manifest lives here, in Python, because it is the prompt's own text — it has
to be readable and reviewable as prose. `tests/test_director_manifest.py` parses
the TypeScript constants and the export's own allow-lists and fails if they have
drifted, so a preset added to the editor cannot silently become something the
director offers before the renderer supports it.
"""

from __future__ import annotations

from typing import Any

#: Schema version the compiler accepts. Bump when a directive's meaning changes.
PLAN_VERSION = 1

#: Enter/exit animations, from `CLIP_ANIMATION_PRESETS` in
#: `_lib/animation/clip-animation.ts`, filtered to those that render.
#:
#: `focus` is excluded: it is a *defocus*, and the export drops the blur channel
#: entirely (C2 in docs/ai_creative_director.md §17.2), so it renders as a bare
#: scale-and-fade — the one thing it is named for is the thing that goes missing.
#: `zoom` is kept despite losing a smaller blur component, because it still reads
#: as a zoom without it; that is a recorded trade-off, not an oversight.
ANIMATION_PRESETS: dict[str, str] = {
    "fade": "Straight fade. The default; invisible, which is usually right.",
    "zoom": "Eases out of a slight push-in. Good for stills that need life.",
    "pop": "Overshoots the scale slightly on entry. Energetic; use sparingly.",
    "slide-left": "Enters from the right, settling leftward.",
    "slide-right": "Enters from the left, settling rightward.",
    "slide-up": "Rises into place. Reads as 'arriving', good under a new point.",
    "spin": "Slight rotation into place. Stylised; rarely right for documentary.",
    "swing": "Pendulum settle. Playful.",
}

#: Interpolation curves for Ken Burns moves. All render identically to the
#: editor preview (see `tests/fixtures/easing_curves.json`).
#:
#: The legacy quadratic `ease-in`/`ease-out`/`ease-in-out` still work everywhere
#: but are not offered: the named curves below supersede them for new authoring,
#: and offering both invites arbitrary choice between curves that differ only
#: subtly. `hold` is a step, which a continuous move never wants.
EASING_CURVES: dict[str, str] = {
    "linear": "No easing. Correct for a slow continuous drift, wrong for a move.",
    "smooth": "Cubic ease-in-out. The workhorse; deliberate without drawing attention.",
    "glide": "Gentle deceleration. Unhurried, documentary.",
    "snappy": "Leaves fast, arrives hard. Energetic, modern.",
    "anticipate": "Pulls back before moving. Draws the eye; use once, not often.",
    "settle": "Overshoots and settles back. Physical, confident.",
    "overshoot": "As settle, but further. Playful; easy to overdo.",
}

#: Picture tracks above the A-roll. V1 is the interview itself and is never a
#: B-roll target.
TRACKS: dict[str, str] = {
    "V2": "The main B-roll layer. Use this unless something must sit over it.",
    "V3": "Above V2. Only for a shot that genuinely overlaps another.",
}

#: From `ASPECT_RATIOS` in `_lib/ai-media-models.ts`. `auto` is excluded: the
#: director is told the project's target aspect and must commit to it, because a
#: 16:9 still in a 9:16 export letterboxes or crops badly (§12).
ASPECT_RATIOS: tuple[str, ...] = ("16:9", "9:16", "4:3", "3:4", "1:1", "21:9")

#: Where a B-roll shot comes from.
#:
#: Two sources are deliberately absent. `stock` is unimplemented and exists in
#: the schema only for forward compatibility (§15.5). `project-media` *is*
#: implementable, but the context never tells the model what footage the project
#: already holds — so choosing it would be guessing at a library it cannot see,
#: and every such directive would resolve to nothing. It comes back when the
#: context carries a media inventory, not before.
ASSET_SOURCES: dict[str, str] = {
    "generate-image": "Generate a still. Cheap and fast; the default.",
    "generate-video": "Generate a moving shot. Minutes per clip and the dominant cost — reserve for moments that genuinely need motion.",
}

#: Directive types the compiler can actually place. `transition`, `text`,
#: `emphasis` and `music` are defined in the schema but not offered here — see
#: docs/ai_creative_director.md §16. v1 is a B-roll director.
DIRECTIVE_TYPES: tuple[str, ...] = ("broll",)

#: Pacing rules. These are enforced in code after the plan comes back, but are
#: also stated in the prompt: a model told the constraint plans within it, while
#: a model whose plan is trimmed afterwards produces incoherent pacing (§8.3).
MIN_GAP_SECONDS = 6.0
MIN_BROLL_SECONDS = 1.2
MAX_BROLL_SECONDS = 8.0
MAX_COVERAGE_RATIO = 0.35


def _bullets(entries: dict[str, str]) -> str:
    return "\n".join(f"  - `{key}` — {value}" for key, value in entries.items())


def render_manifest(*, aspect: str, max_images: int, max_videos: int) -> str:
    """The manifest as prompt text.

    Everything here is stable across runs for a given project shape, which is
    what lets it sit in front of the cache breakpoint while the transcript —
    which changes every run — sits behind it.
    """
    return f"""# Capability manifest

You may only use the values listed here. They are not suggestions; a value not
on these lists cannot be rendered and the directive using it will be discarded.

## Directive types
{_bullets({"broll": "Place a shot over the A-roll. The only directive type available in this version."})}

## Where a shot comes from (`asset.source`)
{_bullets(ASSET_SOURCES)}

## Picture tracks (`track`)
{_bullets(TRACKS)}

## Enter/exit animations (`animationIn.preset`, `animationOut.preset`)
{_bullets(ANIMATION_PRESETS)}

## Easing curves (`framing.kenBurns.easing`)
{_bullets(EASING_CURVES)}

## Aspect ratio (`asset.aspectRatio`)
This project renders at **{aspect}**. Every generated shot must use it.
Available values: {", ".join(f"`{value}`" for value in ASPECT_RATIOS)}

## Budget
- At most **{max_images}** generated stills.
- At most **{max_videos}** generated moving shots.
- B-roll may cover at most **{int(MAX_COVERAGE_RATIO * 100)}%** of the runtime.
- Leave at least **{MIN_GAP_SECONDS:g}s** of A-roll between consecutive shots.
- Each shot runs **{MIN_BROLL_SECONDS:g}–{MAX_BROLL_SECONDS:g}s**.

Plan within the budget rather than over it. A plan that exceeds these is cut to
fit afterwards, and cutting a plan wrecks its pacing — the shots that survive
were chosen to work alongside the ones that did not.
"""


def as_dict() -> dict[str, Any]:
    """Machine-readable form, for the plan schema and the debug viewer."""
    return {
        "planVersion": PLAN_VERSION,
        "directiveTypes": list(DIRECTIVE_TYPES),
        "assetSources": list(ASSET_SOURCES),
        "tracks": list(TRACKS),
        "animationPresets": list(ANIMATION_PRESETS),
        "easingCurves": list(EASING_CURVES),
        "aspectRatios": list(ASPECT_RATIOS),
        "pacing": {
            "minGapSeconds": MIN_GAP_SECONDS,
            "minBrollSeconds": MIN_BROLL_SECONDS,
            "maxBrollSeconds": MAX_BROLL_SECONDS,
            "maxCoverageRatio": MAX_COVERAGE_RATIO,
        },
    }
