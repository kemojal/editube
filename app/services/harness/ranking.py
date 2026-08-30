"""Plan ranking and deterministic fallbacks (plan Phase 5).

The planner chain proposes ONE parameter set, and until now a proposal that
failed to compile was the end of the road — `needs_input` or a failed run,
even when the fix was mechanical (a range slightly past the end of the
video, a span just over the capability cap). This module turns that single
proposal into a small candidate list — the proposal itself plus purely
deterministic repairs — compiles each, scores the survivors, and picks the
best. No second model call, no randomness: the same inputs always choose
the same candidate, and the run records what was considered and why the
winner won, so ranking is inspectable rather than vibes.

Scoring, in order (higher wins):
  1. It compiles at all (non-compilers never win).
  2. Fewer compile warnings — a plan that had to caveat itself ranks below
     one that did not.
  3. More agreement with the user's learned style defaults.
  4. Earlier in the candidate list — the raw proposal outranks its repairs
     on a tie, and repair order is fixed.
"""

from __future__ import annotations

import copy
from typing import Any

from pydantic import ValidationError

from app.services.harness.compiler import CompileError, compile_recipe
from app.services.harness.schemas import HarnessPlan


def candidate_variants(
    params: dict[str, Any],
    *,
    selection: dict[str, float] | None,
    video_duration: float,
    max_clip_seconds: float,
) -> list[tuple[str, dict[str, Any]]]:
    """The proposal plus its deterministic repairs, in fixed rank order.

    Repairs never invent content — they only re-anchor or clamp the range,
    which is the one field a language model reliably gets slightly wrong.
    Identical variants are deduplicated so the ranking never weighs the
    same candidate twice.
    """
    variants: list[tuple[str, dict[str, Any]]] = [("model", params)]

    rng = params.get("range") if isinstance(params.get("range"), dict) else None

    if selection and selection.get("end", 0) > selection.get("start", 0) >= 0:
        repaired = copy.deepcopy(params)
        repaired["range"] = {
            "start": float(selection["start"]),
            "end": float(selection["end"]),
        }
        variants.append(("selection-range", repaired))

    if rng is not None:
        try:
            start = max(0.0, float(rng.get("start", 0.0)))
            end = float(rng.get("end", 0.0))
        except (TypeError, ValueError):
            start, end = 0.0, 0.0
        if end > start:
            if video_duration > 0:
                end = min(end, video_duration)
                start = min(start, max(0.0, end - 0.04))
            if max_clip_seconds > 0 and end - start > max_clip_seconds:
                end = start + max_clip_seconds
            if end > start:
                repaired = copy.deepcopy(params)
                repaired["range"] = {"start": start, "end": end}
                variants.append(("clamped-range", repaired))

    deduped: list[tuple[str, dict[str, Any]]] = []
    seen: list[dict[str, Any]] = []
    for label, candidate in variants:
        if candidate in seen:
            continue
        seen.append(candidate)
        deduped.append((label, candidate))
    return deduped


def choose_candidate(
    recipe_id: str,
    variants: list[tuple[str, dict[str, Any]]],
    *,
    capability_snapshot: dict[str, Any],
    video_duration: float,
    draft: dict[str, Any] | None,
    learned: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], HarnessPlan, dict[str, Any]] | tuple[None, None, dict[str, Any]]:
    """Compile and rank every variant; return (params, plan, report).

    The report lists every candidate with its outcome — the inspectable
    trail. When nothing compiles, `(None, None, report)` comes back and the
    first candidate's error is the honest one to surface (it is the model's
    own proposal; the repairs failing too is detail, not headline).
    """
    learned = learned or {}
    considered: list[dict[str, Any]] = []
    best: tuple[tuple[int, int, int], str, dict[str, Any], HarnessPlan] | None = None
    for index, (label, candidate) in enumerate(variants):
        try:
            plan = compile_recipe(
                recipe_id,
                candidate,
                capability_snapshot=capability_snapshot,
                video_duration=video_duration,
                draft=draft,
            )
        except CompileError as exc:
            considered.append({"candidate": label, "outcome": exc.code})
            continue
        except ValidationError:
            considered.append({"candidate": label, "outcome": "invalid_params"})
            continue
        agreement = sum(
            1 for key, value in learned.items() if candidate.get(key) == value
        )
        score = (-len(plan.warnings), agreement, -index)
        considered.append(
            {
                "candidate": label,
                "outcome": "compiled",
                "warnings": len(plan.warnings),
                "learnedAgreement": agreement,
            }
        )
        if best is None or score > best[0]:
            best = (score, label, candidate, plan)

    if best is None:
        return None, None, {"considered": considered, "chosen": None}
    _, label, candidate, plan = best
    return candidate, plan, {"considered": considered, "chosen": label}
