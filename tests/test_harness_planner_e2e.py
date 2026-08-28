"""Live end-to-end: OpenRouter free model → recipe params → compiled plan.

Runs only when `OPENROUTER_API_KEY` is configured (skipped otherwise, so CI
without credentials stays green). Uses the best free model OpenRouter offers
at run time — per the product decision that model-assisted e2e testing rides
the free tier.

The assertion is the whole point of the harness contract: whatever the model
says must survive `compile_recipe`'s validation unchanged, or the plan is
rejected loudly — never repaired silently.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("OPENROUTER_API_KEY", "").strip()
    or os.getenv("HARNESS_E2E_OFFLINE") == "1",
    reason="OPENROUTER_API_KEY not configured (or HARNESS_E2E_OFFLINE=1)",
)

SEG_SNAPSHOT = {
    "capabilities": {
        "segmentation": {
            "key": "segmentation",
            "available": True,
            "limits": {"maxClipSeconds": 120},
            "detail": {"autoMatte": True, "pointPrompt": False, "propagate": False},
        }
    }
}


def test_free_model_catalog_yields_a_model():
    from app.services.harness.planner import best_free_model

    model = best_free_model()
    assert model and isinstance(model, str)


def test_intent_to_validated_plan_end_to_end():
    from app.services.harness.compiler import compile_recipe
    from app.services.harness.planner import plan_subject_behind_text

    result = plan_subject_behind_text(
        "Put me behind a big title that says LAUNCH DAY during the intro",
        video_duration=42.0,
        selection={"start": 3.0, "end": 9.5},
    )
    assert result.recipe_id == "subject_behind_text"
    assert result.model

    plan = compile_recipe(
        "subject_behind_text",
        result.params,
        capability_snapshot=SEG_SNAPSHOT,
        video_duration=42.0,
    )
    ops = {op.type for op in plan.operations}
    assert ops == {
        "timeline.duplicate_linked",
        "visual.apply_subject_mask",
        "overlay.create_text",
    }
    text_op = next(op for op in plan.operations if op.type == "overlay.create_text")
    assert "launch" in text_op.text.lower()
    assert 0 <= text_op.range.start < text_op.range.end <= 42.5
