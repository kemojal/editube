"""The EditPlan contract, and the rules the model is not trusted to keep.

Forcing a tool call gets the plan back *shape*-valid: the API validates it
against the schema below and re-prompts the model on a mismatch, so nothing here
has to parse prose or repair JSON. What the schema cannot express is everything
relational — that shots do not overlap, that they leave room to breathe, that
the budget was respected, that an anchor points at words the transcript actually
contains. Those are checked here, after the fact, in code.

The split matters for a reason beyond tidiness. Pacing rules stated only in the
prompt get *mostly* followed; pacing rules enforced only in code produce plans
that were coherent before trimming and arbitrary after. So they are both: the
prompt states them so the model plans within them (§8.3), and
`validate_plan` enforces them so a plan that ignored them cannot reach the
compiler. When those disagree, the enforcement wins and says so in a warning —
a dropped shot the user can see beats a silent one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.services import director_manifest as manifest

logger = logging.getLogger(__name__)


def _enum(values: Any) -> list[str]:
    return list(values)


#: JSON Schema for the forced tool call.
#:
#: Every enum is generated from the manifest rather than written out again, so
#: a value withheld there cannot be offered here. `additionalProperties: false`
#: is required for `strict` tool use and is applied by `claude_client.build_tool`
#: at the top level; nested objects declare it themselves.
def plan_schema() -> dict[str, Any]:
    anchor = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "quote": {
                "type": "string",
                "description": (
                    "The exact words this shot sits over, copied verbatim from the "
                    "transcript. This is how the shot stays attached to the moment "
                    "if the cut changes later, so it must be a real quote, not a "
                    "paraphrase."
                ),
            },
            "segmentId": {
                "type": "string",
                "description": (
                    "The `[sN]` id of the transcript line the quote is from. A "
                    "wrong id is recoverable — the quote is searched for "
                    "everywhere before the anchor is given up on — but a wrong "
                    "quote is not."
                ),
            },
        },
        "required": ["quote", "segmentId"],
    }

    asset = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "source": {"type": "string", "enum": _enum(manifest.ASSET_SOURCES)},
            "prompt": {
                "type": "string",
                "description": (
                    "What the shot shows. Describe the image, not the idea: a "
                    "camera could photograph 'an overhead shot of a cluttered "
                    "desk at dusk' and could not photograph 'the concept of "
                    "wasted time'. Do not restate the house style — it is added "
                    "for you, identically, to every shot."
                ),
            },
            "aspectRatio": {"type": "string", "enum": _enum(manifest.ASPECT_RATIOS)},
            "durationSeconds": {
                "type": "number",
                "minimum": manifest.MIN_BROLL_SECONDS,
                "maximum": manifest.MAX_BROLL_SECONDS,
            },
        },
        "required": ["source", "prompt", "aspectRatio"],
    }

    ken_burns = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "from": {"type": "number", "minimum": 1.0, "maximum": 1.4},
            "to": {"type": "number", "minimum": 1.0, "maximum": 1.4},
            "easing": {"type": "string", "enum": _enum(manifest.EASING_CURVES)},
        },
        "required": ["from", "to", "easing"],
    }

    animation = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "preset": {"type": "string", "enum": _enum(manifest.ANIMATION_PRESETS)},
            "durationSeconds": {"type": "number", "minimum": 0.12, "maximum": 1.5},
        },
        "required": ["preset", "durationSeconds"],
    }

    directive = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "string"},
            "type": {"type": "string", "enum": _enum(manifest.DIRECTIVE_TYPES)},
            "start": {"type": "number", "minimum": 0},
            "end": {"type": "number", "minimum": 0},
            "anchor": anchor,
            "track": {"type": "string", "enum": _enum(manifest.TRACKS)},
            "asset": asset,
            "framing": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"kenBurns": ken_burns},
                "required": ["kenBurns"],
            },
            "animationIn": animation,
            "animationOut": animation,
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "why": {
                "type": "string",
                "description": (
                    "Why this shot, here, in one sentence. The user reads this. "
                    "'Speaker names three cities; show a map' is a reason; "
                    "'adds visual interest' is not."
                ),
            },
        },
        "required": [
            "id", "type", "start", "end", "anchor", "track", "asset",
            "animationIn", "animationOut", "confidence", "why",
        ],
    }

    beat = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "string"},
            "kind": {
                "type": "string",
                "enum": ["hook", "context", "point", "example", "pivot", "cta", "outro"],
            },
            "start": {"type": "number", "minimum": 0},
            "end": {"type": "number", "minimum": 0},
            "summary": {"type": "string"},
        },
        "required": ["id", "kind", "start", "end", "summary"],
    }

    brief = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "genre": {"type": "string"},
            "audience": {"type": "string"},
            "tone": {"type": "array", "items": {"type": "string"}},
            "pacing": {"type": "string", "enum": ["slow", "medium", "fast"]},
            "visualMotifs": {"type": "array", "items": {"type": "string"}},
            "houseStylePrefix": {
                "type": "string",
                "description": (
                    "A director-of-photography brief prepended verbatim to every "
                    "generated shot: lens and focal length, lighting direction and "
                    "quality, colour temperature, depth of field, grain. This is "
                    "what makes twelve generated images look like one film instead "
                    "of twelve stock photos, so it must be specific and it must "
                    "not vary between shots."
                ),
            },
            "rationale": {"type": "string"},
        },
        "required": [
            "genre", "audience", "tone", "pacing", "visualMotifs",
            "houseStylePrefix", "rationale",
        ],
    }

    return {
        "type": "object",
        "properties": {
            "version": {"type": "integer", "enum": [manifest.PLAN_VERSION]},
            "brief": brief,
            "beats": {"type": "array", "items": beat},
            "directives": {"type": "array", "items": directive},
        },
        "required": ["version", "brief", "beats", "directives"],
    }


def brief_schema() -> dict[str, Any]:
    """Pass A's tool: the treatment, before any shot is chosen."""
    full = plan_schema()
    return {
        "type": "object",
        "properties": {"brief": full["properties"]["brief"], "beats": full["properties"]["beats"]},
        "required": ["brief", "beats"],
    }


def directives_schema() -> dict[str, Any]:
    """Pass B's tool: the shots, given the treatment from pass A."""
    full = plan_schema()
    return {
        "type": "object",
        "properties": {"directives": full["properties"]["directives"]},
        "required": ["directives"],
    }


@dataclass
class ValidatedPlan:
    """A plan the compiler may act on, plus what was dropped getting there."""

    version: int
    brief: dict[str, Any]
    beats: list[dict[str, Any]]
    directives: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "brief": self.brief,
            "beats": self.beats,
            "directives": self.directives,
            "warnings": self.warnings,
        }


class PlanRejected(RuntimeError):
    """The plan is unusable as a whole — not merely missing some shots."""


def validate_plan(
    raw: dict[str, Any],
    *,
    runtime_seconds: float,
    context: Any | None = None,
    max_images: int,
    max_videos: int,
) -> ValidatedPlan:
    """Enforce everything the schema cannot express.

    Directives are dropped rather than repaired. A shot whose timing or anchor
    cannot be trusted is a shot in the wrong place, and a B-roll cut landing
    mid-syllable is more damaging than the shot simply not being there.
    """
    version = raw.get("version")
    if version != manifest.PLAN_VERSION:
        # Refusing an unknown version is the point of versioning it: a plan
        # written against different directive semantics must not be compiled
        # by a compiler that reads them differently.
        raise PlanRejected(f"Unsupported plan version {version!r}")

    brief = raw.get("brief")
    if not isinstance(brief, dict) or not str(brief.get("houseStylePrefix") or "").strip():
        raise PlanRejected("Plan has no house style; generated shots would not cohere")

    warnings: list[str] = []
    kept: list[dict[str, Any]] = []
    images = videos = 0
    seen_ids: set[str] = set()

    def drop(directive: Any, reason: str) -> None:
        warnings.append(f"Dropped {_label(directive)}: {reason}")

    for directive in raw.get("directives") or []:
        if not isinstance(directive, dict):
            continue

        directive_id = str(directive.get("id") or "")
        if not directive_id or directive_id in seen_ids:
            drop(directive, "duplicate or missing id")
            continue

        try:
            start = float(directive["start"])
            end = float(directive["end"])
        except (KeyError, TypeError, ValueError):
            drop(directive, "unreadable timing")
            continue

        span = end - start
        if span < manifest.MIN_BROLL_SECONDS:
            drop(directive, f"too short ({span:.2f}s)")
            continue
        if span > manifest.MAX_BROLL_SECONDS:
            # Trimmed rather than dropped: the moment it was chosen for is still
            # right, it was only asked to last too long.
            end = start + manifest.MAX_BROLL_SECONDS
            span = manifest.MAX_BROLL_SECONDS
            warnings.append(f"Shortened {_label(directive)} to {span:g}s")
        if start < 0 or end > runtime_seconds + 0.5:
            drop(directive, "falls outside the cut")
            continue

        anchor = directive.get("anchor")
        quote = str(anchor.get("quote") or "").strip() if isinstance(anchor, dict) else ""
        if not quote:
            drop(directive, "no anchor quote")
            continue
        if context is not None:
            from app.services.director_context import resolve_anchor

            resolved = resolve_anchor(
                context,
                segment_id=str(anchor.get("segmentId") or ""),
                quote=quote,
                fallback_director_start=start,
                fallback_director_end=end,
            )
            if not resolved.exact:
                # A misquote is worse than no anchor: it reads as precise and
                # would attach the shot to whatever the timing happened to hit.
                # `resolve_anchor` already searched every segment, so this means
                # the words genuinely are not in the transcript.
                drop(directive, "anchor quotes words not in the transcript")
                continue

        asset = directive.get("asset")
        if not isinstance(asset, dict):
            drop(directive, "no asset")
            continue
        source = str(asset.get("source") or "")
        if source == "generate-video":
            if videos >= max_videos:
                drop(directive, "over the moving-shot budget")
                continue
            videos += 1
        elif source == "generate-image":
            if images >= max_images:
                drop(directive, "over the still budget")
                continue
            images += 1

        # Overlap and breathing room are checked against what actually survived,
        # not against the model's own list, so a dropped shot frees the gap it
        # was occupying instead of pushing its neighbour out too.
        if kept:
            previous_end = float(kept[-1]["end"])
            if start < previous_end:
                drop(directive, "overlaps the previous shot")
                continue
            if start - previous_end < manifest.MIN_GAP_SECONDS:
                drop(directive, f"only {start - previous_end:.1f}s after the previous shot")
                continue

        seen_ids.add(directive_id)
        kept.append({**directive, "start": start, "end": end})

    coverage_limit = runtime_seconds * manifest.MAX_COVERAGE_RATIO
    covered = 0.0
    within_coverage: list[dict[str, Any]] = []
    for directive in kept:
        span = float(directive["end"]) - float(directive["start"])
        if covered + span > coverage_limit:
            warnings.append(f"Dropped {_label(directive)}: over the coverage budget")
            continue
        covered += span
        within_coverage.append(directive)

    beats = [b for b in (raw.get("beats") or []) if isinstance(b, dict)]
    if not within_coverage:
        warnings.append("No shot survived validation; the cut is left as-is.")

    return ValidatedPlan(
        version=manifest.PLAN_VERSION,
        brief=brief,
        beats=beats,
        directives=within_coverage,
        warnings=warnings,
    )


def _label(directive: Any) -> str:
    """Name a directive the way the user would recognise it."""
    if not isinstance(directive, dict):
        return "a directive"
    anchor = directive.get("anchor")
    quote = str(anchor.get("quote") or "") if isinstance(anchor, dict) else ""
    if quote:
        trimmed = quote if len(quote) <= 40 else f"{quote[:39]}…"
        return f'the shot over "{trimmed}"'
    return f"directive {directive.get('id') or '?'}"
