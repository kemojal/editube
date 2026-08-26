"""Running the director: context in, validated plan out.

This is the seam between "talking to a model" and "producing something the
compiler can act on". Everything above it (`claude_client`) knows nothing about
video; everything below it (`director_plan`, `director_compile`) knows nothing
about Claude. Keeping the boundary here is what makes the planner testable
without an API key and the compiler testable without a plan.

The two passes run as one conversation so the system prompt and the transcript —
the bulk of the tokens — are read from cache on the second pass rather than
re-billed. See `Conversation` for the mechanics.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.services import claude_client, director_manifest as manifest, director_prompts
from app.services.director_context import DirectorContext
from app.services.director_plan import (
    PlanRejected,
    ValidatedPlan,
    brief_schema,
    directives_schema,
    validate_plan,
)

logger = logging.getLogger(__name__)

#: Budget tiers offered in the wizard. Images are cheap and fast; moving shots
#: cost minutes of worker time each and dominate the bill, so they scale far
#: more conservatively than the still count does.
BUDGET_TIERS: dict[str, tuple[int, int]] = {
    "light": (6, 0),
    "standard": (12, 1),
    "rich": (20, 3),
}


@dataclass
class DirectorOptions:
    """What the user asked for, from the wizard."""

    tier: str = "standard"
    brief: str = ""
    #: Moving shots are opt-in even at tiers that allow them: they are the
    #: dominant cost and the slowest stage, and plenty of pieces do not want any.
    allow_video: bool = True

    @property
    def budget(self) -> tuple[int, int]:
        images, videos = BUDGET_TIERS.get(self.tier, BUDGET_TIERS["standard"])
        return images, (videos if self.allow_video else 0)


@dataclass
class DirectorRun:
    """A completed planning run, including what it cost."""

    plan: ValidatedPlan
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {**self.plan.to_dict(), "usage": self.usage, "model": self.model}


class DirectorUnavailable(RuntimeError):
    """The director cannot run here — no key, or nothing to work from."""


def generate_plan(
    context: DirectorContext,
    options: DirectorOptions | None = None,
) -> DirectorRun:
    """Read the piece, direct it, and return a plan the compiler may act on."""
    options = options or DirectorOptions()

    if not claude_client.available():
        raise DirectorUnavailable("ANTHROPIC_API_KEY is not set")
    if not context.has_speech:
        # Everything the director does is anchored to something that was said.
        # A silent video has nothing to hang a shot on, and guessing from
        # picture alone is a different feature.
        raise DirectorUnavailable("This video has no transcript to direct from")

    max_images, max_videos = options.budget
    system = director_prompts.system_prompt(
        aspect=context.aspect,
        max_images=max_images,
        max_videos=max_videos,
        brief=options.brief,
    )
    conversation = claude_client.Conversation(system)

    # Pass A — read the piece and commit to a treatment. Runs at the default
    # effort: this is the judgement call the rest of the run is built on, and
    # the cheapest place to spend thinking.
    treatment = conversation.ask(
        director_prompts.read_prompt(
            transcript=context.transcript, runtime_seconds=context.runtime_seconds
        ),
        tool=claude_client.build_tool(
            "submit_treatment",
            "Record what this piece is and how it is structured, before choosing any shots.",
            brief_schema(),
        ),
    )

    # Pass B — choose the shots. Reads the whole prefix from cache.
    shots = conversation.ask(
        director_prompts.direct_prompt(
            runtime_seconds=context.runtime_seconds,
            max_images=max_images,
            max_videos=max_videos,
        ),
        tool=claude_client.build_tool(
            "submit_directives",
            "Choose the moments that earn a shot, and say what each shot is.",
            directives_schema(),
        ),
    )

    raw = {
        "version": manifest.PLAN_VERSION,
        "brief": treatment.get("brief", {}),
        "beats": treatment.get("beats", []),
        "directives": shots.get("directives", []),
    }

    plan = validate_plan(
        raw,
        runtime_seconds=context.runtime_seconds,
        context=context,
        max_images=max_images,
        max_videos=max_videos,
    )

    if not conversation.usage.cached:
        # Not fatal, but it means the second pass re-billed the transcript. On a
        # long video that is most of the cost of the run, and it is invisible
        # unless someone says so.
        logger.warning(
            "Director run read nothing from cache; the prefix is being invalidated between passes"
        )

    logger.info(
        "Director planned %s shot(s) for a %.0fs cut (%s dropped, %s output tokens)",
        len(plan.directives),
        context.runtime_seconds,
        len(plan.warnings),
        conversation.usage.output_tokens,
    )
    return DirectorRun(
        plan=plan,
        usage=conversation.usage.to_dict(),
        model=conversation.model,
    )


__all__ = [
    "BUDGET_TIERS",
    "DirectorOptions",
    "DirectorRun",
    "DirectorUnavailable",
    "PlanRejected",
    "generate_plan",
]
