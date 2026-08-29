"""The non-additive operation family: modifying the user's own clips.

This is where `restore_value` inverses earn their keep — the Director's
manifest-filter revert only ever worked because it never touched existing
state. These ops do, and revert must put back EXACTLY what was there.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.services.harness.compiler import compile_recipe
from app.services.harness.mutations import MutationContext, apply_plan, revert_manifest
from app.services.harness.schemas import HarnessPlan

SEG_OK = {
    "capabilities": {
        "segmentation": {
            "key": "segmentation",
            "available": True,
            "limits": {"maxClipSeconds": 120},
            "detail": {"autoMatte": True},
        }
    }
}

DRAFT = {
    "keepRanges": [
        {"id": "r1", "start": 0.0, "end": 10.0},
        {"id": "r2", "start": 10.0, "end": 20.0},
    ],
    "clipAttributes": {
        "video:r1": {"adjust": {"exposure": 30.0, "vignette": 5.0}},
    },
}


def _adjust_plan(**op_overrides) -> HarnessPlan:
    return HarnessPlan.model_validate(
        {
            "recipe": "x",
            "operations": [
                {
                    "id": "dim",
                    "type": "visual.adjust",
                    "clipKey": "video:r1",
                    "fingerprint": {"start": 0.0, "end": 10.0},
                    "settings": {"exposure": -22.0, "saturation": -12.0},
                    **op_overrides,
                }
            ],
        }
    )


def _ctx() -> MutationContext:
    return MutationContext(run_id=9, video_id=3, video_duration=20.0)


class TestSchema:
    def test_unknown_adjust_keys_are_rejected(self):
        with pytest.raises(ValidationError, match="unknown adjust key"):
            _adjust_plan(settings={"lut": 1.0})

    def test_out_of_slider_values_are_rejected(self):
        with pytest.raises(ValidationError, match="slider range"):
            _adjust_plan(settings={"exposure": 500.0})

    def test_audio_op_needs_something_to_do(self):
        with pytest.raises(ValidationError, match="at least one"):
            HarnessPlan.model_validate(
                {
                    "recipe": "x",
                    "operations": [
                        {"id": "a", "type": "audio.adjust", "clipKey": "video:r1"}
                    ],
                }
            )


class TestAdjustExistingClip:
    def test_merges_over_the_users_grade_and_revert_restores_it_exactly(self):
        applied = apply_plan(DRAFT, _adjust_plan(), _ctx())
        merged = applied.draft["clipAttributes"]["video:r1"]["adjust"]
        # The user's own keys survive the merge…
        assert merged["vignette"] == 5.0
        # …ours land on top.
        assert merged["exposure"] == -22.0 and merged["saturation"] == -12.0

        reverted, warnings = revert_manifest(applied.draft, applied.inverse)
        assert warnings == []
        assert reverted == DRAFT

    def test_revert_refuses_after_the_user_regrades(self):
        applied = apply_plan(DRAFT, _adjust_plan(), _ctx())
        # The user tweaks exposure again AFTER the run applied.
        applied.draft["clipAttributes"]["video:r1"]["adjust"]["exposure"] = 50.0
        reverted, warnings = revert_manifest(applied.draft, applied.inverse)
        assert reverted["clipAttributes"]["video:r1"]["adjust"]["exposure"] == 50.0
        assert any("changed after this run" in w for w in warnings)

    def test_a_recut_clip_is_skipped_not_modified_approximately(self):
        moved = {
            **DRAFT,
            "keepRanges": [{"id": "r1", "start": 4.0, "end": 6.0}],
        }
        applied = apply_plan(moved, _adjust_plan(), _ctx())
        assert applied.draft == moved  # untouched
        assert any("re-cut" in w for w in applied.warnings)

    def test_one_preserved_edge_still_counts_as_the_same_clip(self):
        trimmed = {**DRAFT, "keepRanges": [{"id": "r1", "start": 0.0, "end": 7.5}]}
        applied = apply_plan(trimmed, _adjust_plan(), _ctx())
        assert applied.draft["clipAttributes"]["video:r1"]["adjust"]["exposure"] == -22.0

    def test_a_deleted_clip_is_skipped_with_the_reason(self):
        gone = {**DRAFT, "keepRanges": [{"id": "r9", "start": 0.0, "end": 10.0}]}
        applied = apply_plan(gone, _adjust_plan(), _ctx())
        assert any("deleted" in w for w in applied.warnings)


class TestAudioExistingClip:
    def test_audio_merge_and_exact_revert(self):
        plan = HarnessPlan.model_validate(
            {
                "recipe": "x",
                "operations": [
                    {
                        "id": "level",
                        "type": "audio.adjust",
                        "clipKey": "video:r2",
                        "fingerprint": {"start": 10.0, "end": 20.0},
                        "volume": -6.0,
                        "fadeIn": 0.3,
                    }
                ],
            }
        )
        applied = apply_plan(DRAFT, plan, _ctx())
        audio = applied.draft["clipAttributes"]["video:r2"]["audio"]
        assert audio == {"volume": -6.0, "fadeIn": 0.3}
        reverted, warnings = revert_manifest(applied.draft, applied.inverse)
        assert warnings == []
        assert reverted == DRAFT


class TestRecipeDimBackground:
    def _compile(self, draft):
        return compile_recipe(
            "subject_behind_text",
            {
                "range": {"start": 2.0, "end": 8.0},
                "text": "BIG IDEA",
                "dimBackground": True,
            },
            capability_snapshot=SEG_OK,
            video_duration=20.0,
            draft=draft,
        )

    def test_emits_the_dim_op_against_the_base_clip(self):
        plan = self._compile(DRAFT)
        dim = next(op for op in plan.operations if op.type == "visual.adjust")
        assert dim.clipKey == "video:r1"
        assert dim.fingerprint.start == 0.0 and dim.fingerprint.end == 10.0
        assert dim.settings["exposure"] < 0

    def test_degrades_with_a_warning_when_the_range_has_no_id(self):
        plan = self._compile({"keepRanges": [{"start": 0.0, "end": 10.0}]})
        assert all(op.type != "visual.adjust" for op in plan.operations)
        assert any("no addressable id" in w for w in plan.warnings)

    def test_off_by_default(self):
        plan = compile_recipe(
            "subject_behind_text",
            {"range": {"start": 2.0, "end": 8.0}, "text": "T"},
            capability_snapshot=SEG_OK,
            video_duration=20.0,
            draft=DRAFT,
        )
        assert all(op.type != "visual.adjust" for op in plan.operations)
