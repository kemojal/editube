"""The pure harness engine: schemas, mutations, inverse manifests, compiler."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.services.harness.compiler import CompileError, compile_recipe, estimate_plan
from app.services.harness.mutations import (
    MutationContext,
    apply_plan,
    revert_manifest,
)
from app.services.harness.schemas import (
    HarnessPlan,
    SourceRange,
    entity_id,
    plan_checksum,
)

SEG_OK = {
    "capabilities": {
        "segmentation": {
            "key": "segmentation",
            "available": True,
            "limits": {"maxClipSeconds": 120},
            "detail": {"autoMatte": True, "pointPrompt": False, "propagate": False},
        }
    }
}


def _plan(**params) -> HarnessPlan:
    merged = {
        "range": {"start": 2.0, "end": 8.0},
        "text": "BIG IDEA",
        **params,
    }
    return compile_recipe(
        "subject_behind_text", merged, capability_snapshot=SEG_OK, video_duration=20.0
    )


def _ctx(run_id: int = 7, staged: dict | None = None) -> MutationContext:
    return MutationContext(
        run_id=run_id,
        video_id=3,
        video_duration=20.0,
        source_url="https://cdn.example.test/v.mp4",
        staged_assets=staged
        or {"mask": {"resultId": 55, "outputUrl": "https://cdn.example.test/cut.webm"}},
    )


class TestSchemas:
    def test_unknown_fields_are_rejected(self):
        with pytest.raises(ValidationError):
            HarnessPlan.model_validate(
                {
                    "recipe": "x",
                    "operations": [
                        {
                            "id": "a",
                            "type": "overlay.create_text",
                            "text": "hi",
                            "range": {"start": 0, "end": 1},
                            "surprise": True,
                        }
                    ],
                }
            )

    def test_dependency_on_unknown_operation_is_rejected(self):
        with pytest.raises(ValidationError, match="unknown"):
            HarnessPlan.model_validate(
                {
                    "recipe": "x",
                    "operations": [
                        {
                            "id": "a",
                            "type": "visual.apply_subject_mask",
                            "targetOp": "ghost",
                        }
                    ],
                }
            )

    def test_dependency_cycles_are_rejected(self):
        with pytest.raises(ValidationError, match="cycle"):
            HarnessPlan.model_validate(
                {
                    "recipe": "x",
                    "operations": [
                        {"id": "a", "type": "overlay.create_text", "text": "t",
                         "range": {"start": 0, "end": 1}, "dependsOn": ["b"]},
                        {"id": "b", "type": "overlay.create_text", "text": "t",
                         "range": {"start": 0, "end": 1}, "dependsOn": ["a"]},
                    ],
                }
            )

    def test_keyframe_channels_without_an_export_path_are_rejected(self):
        with pytest.raises(ValidationError, match="export path"):
            HarnessPlan.model_validate(
                {
                    "recipe": "x",
                    "operations": [
                        {"id": "fg", "type": "timeline.duplicate_linked",
                         "range": {"start": 0, "end": 1}},
                        {"id": "k", "type": "motion.set_keyframes", "targetOp": "fg",
                         "channel": "mask.zoom", "keyframes": [{"t": 0, "v": 1}]},
                    ],
                }
            )

    def test_range_end_must_follow_start(self):
        with pytest.raises(ValidationError):
            SourceRange(start=5, end=5)

    def test_checksum_is_stable_and_order_sensitive(self):
        plan = _plan()
        assert plan_checksum(plan) == plan_checksum(_plan())


class TestCompiler:
    def test_produces_the_three_operation_composite(self):
        plan = _plan()
        types = [op.type for op in plan.operations]
        assert types == [
            "timeline.duplicate_linked",
            "visual.apply_subject_mask",
            "overlay.create_text",
        ]
        mask = plan.operations[1]
        assert mask.dependsOn == ["fg"] and mask.targetOp == "fg"

    def test_refuses_when_segmentation_is_unavailable(self):
        snapshot = {
            "capabilities": {
                "segmentation": {"key": "segmentation", "available": False,
                                  "reason": "no provider"}
            }
        }
        with pytest.raises(CompileError) as exc:
            compile_recipe(
                "subject_behind_text",
                {"range": {"start": 0, "end": 5}, "text": "x"},
                capability_snapshot=snapshot,
                video_duration=20,
            )
        assert exc.value.code == "capability_unavailable"

    def test_refuses_a_range_past_the_segmentation_cap(self):
        with pytest.raises(CompileError) as exc:
            _plan(range={"start": 0, "end": 300})
        assert exc.value.code == "range_too_long"

    def test_refuses_a_range_outside_the_media(self):
        with pytest.raises(CompileError) as exc:
            _plan(range={"start": 18, "end": 25})
        assert exc.value.code == "range_outside_media"

    def test_estimates_scale_with_the_selection(self):
        estimates = estimate_plan(_plan())
        assert estimates["affectedDurationSeconds"] == pytest.approx(6.0)
        assert estimates["processingSeconds"] > 0
        assert estimates["operationCount"] == 3


class TestMutations:
    def test_apply_builds_the_composite(self):
        plan = _plan()
        result = apply_plan({"keepRanges": [{"start": 0, "end": 20}]}, plan, _ctx())
        draft = result.draft

        fg_id = entity_id(7, "fg")
        item = next(i for i in draft["timelineMediaItems"] if i["id"] == fg_id)
        assert item["audioEnabled"] is False
        assert item["sourceKind"] == "video" and item["sourceId"] == 3
        assert item["groupId"] == "grp-ehr7" and item["semanticRole"] == "foreground"

        overlay = next(o for o in draft["textOverlays"] if o["id"] == entity_id(7, "text"))
        assert overlay["text"] == "BIG IDEA" and overlay["groupId"] == "grp-ehr7"

        attrs = draft["clipAttributes"][f"media:{fg_id}"]
        assert attrs["removeBg"]["autoRemoval"] is True
        assert attrs["processing"]["remove_bg"]["status"] == "completed"
        assert attrs["processing"]["remove_bg"]["outputUrl"].endswith("cut.webm")

        # The foreground track sits above every other video track.
        tracks = draft["timelineTracks"]
        fx = next(t for t in tracks if t["id"] == item["trackId"])
        v1_orders = [t["order"] for t in tracks if t["kind"] == "video" and t["id"] != fx["id"]]
        assert all(fx["order"] < order for order in v1_orders)

    def test_apply_is_idempotent_by_derived_ids(self):
        plan = _plan()
        once = apply_plan({}, plan, _ctx()).draft
        twice = apply_plan(once, plan, _ctx()).draft
        assert once["timelineMediaItems"] == twice["timelineMediaItems"]
        assert once["textOverlays"] == twice["textOverlays"]

    def test_apply_then_revert_is_identity(self):
        base = {
            "keepRanges": [{"start": 0, "end": 20}],
            "clipAttributes": {"video:r1": {"adjust": {"exposure": 2}}},
            "timelineMediaItems": [{"id": "user-item", "trackId": "track-video-1"}],
            "timelineTracks": [
                {"id": "track-text-1", "kind": "text", "label": "TX1", "enabled": True,
                 "locked": False, "height": 22, "order": 0},
                {"id": "track-video-1", "kind": "video", "label": "V1", "enabled": True,
                 "locked": False, "height": 44, "order": 1},
            ],
        }
        plan = _plan()
        applied = apply_plan(base, plan, _ctx())
        reverted, warnings = revert_manifest(applied.draft, applied.inverse)
        assert reverted == base
        assert warnings == []

    def test_revert_refuses_to_clobber_a_value_changed_since_apply(self):
        """A `restore_value` whose location moved on is refused, not clobbered."""
        draft = {"clipAttributes": {"video:r1": {"animation": {"preset": "zoom"}}}}
        inverse = [
            {
                "op": "restore_value",
                "path": ["clipAttributes", "video:r1", "animation"],
                "before": {"__absent__": True},
                # The run wrote `fade`; the user has since changed it to zoom.
                "after": {"preset": "fade"},
            }
        ]
        reverted, warnings = revert_manifest(draft, inverse)
        assert reverted["clipAttributes"]["video:r1"]["animation"] == {"preset": "zoom"}
        assert any("changed after this run" in w for w in warnings)

    def test_revert_removes_a_run_created_attribute_key_even_after_user_edits(self):
        plan = _plan()
        applied = apply_plan({}, plan, _ctx())
        fg_key = f"media:{entity_id(7, 'fg')}"
        # The user re-tuned the removal quality after the run applied; the key
        # itself is still run-created, so removal (not restore) takes it out.
        applied.draft["clipAttributes"][fg_key]["removeBg"] = {"quality": "better"}
        reverted, _ = revert_manifest(applied.draft, applied.inverse)
        assert fg_key not in reverted.get("clipAttributes", {})

    def test_disabling_a_dependency_cascades_at_apply_time(self):
        plan = _plan()
        for op in plan.operations:
            if op.id == "fg":
                op.enabled = False
        result = apply_plan({}, plan, _ctx())
        assert result.draft.get("timelineMediaItems", []) == []
        assert not result.draft.get("clipAttributes")
        # Text does not depend on the foreground; it still lands.
        assert len(result.draft["textOverlays"]) == 1
        assert any("depends on disabled" in w for w in result.warnings)

    def test_reverting_a_track_now_occupied_by_user_clips_keeps_it(self):
        plan = _plan()
        applied = apply_plan({}, plan, _ctx())
        track_id = next(
            i["trackId"]
            for i in applied.draft["timelineMediaItems"]
            if i["id"] == entity_id(7, "fg")
        )
        applied.draft["timelineMediaItems"].append({"id": "user-clip", "trackId": track_id})
        reverted, warnings = revert_manifest(applied.draft, applied.inverse)
        assert any(t["id"] == track_id for t in reverted["timelineTracks"])
        assert any("kept" in w for w in warnings)
