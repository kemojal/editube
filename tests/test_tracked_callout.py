"""The tracked-callout recipe: compile gating, staging fakes, the full loop."""

from __future__ import annotations

import pytest

from app.services.harness import executor as executor_module
from app.services.harness.compiler import CompileError, compile_recipe
from app.services.harness.label_card import render_label_card
from app.services.harness.mutations import MutationContext, apply_plan, revert_manifest
from app.services.harness.schemas import entity_id

CAPS = {
    "capabilities": {
        "tracking": {"key": "tracking", "available": True, "provider": "sam2-propagate"},
        "storage": {"key": "storage", "available": True},
        "segmentation": {
            "key": "segmentation", "available": True, "limits": {"maxClipSeconds": 120},
        },
    }
}

PARAMS = {
    "range": {"start": 2.0, "end": 8.0},
    "box": {"x": 12.0, "y": -5.0, "width": 18.0, "height": 22.0},
    "label": "New feature",
    "accent": "#FFD400",
}

TRACK_KEYFRAMES = [
    {"frame": 60, "t": 0.0, "x": 12.0, "y": -5.0, "width": 18.0, "height": 22.0},
    {"frame": 90, "t": 1.0, "x": 20.0, "y": -3.0, "width": 18.0, "height": 22.0},
    {"frame": 120, "t": 2.0, "x": 55.0, "y": 0.0, "width": 18.0, "height": 22.0},
]


def _plan(params=PARAMS, caps=CAPS):
    return compile_recipe(
        "tracked_callout", params, capability_snapshot=caps, video_duration=30.0
    )


class TestCompile:
    def test_produces_the_four_op_graph(self):
        plan = _plan()
        assert [op.type for op in plan.operations] == [
            "analysis.track_object",
            "media.stage_label",
            "timeline.place_label",
            "motion.track_keyframes",
        ]
        follow = plan.operations[3]
        assert follow.trackOp == "track" and follow.targetOp == "place"
        # Object sits right of centre → the card goes left, offset negative.
        assert follow.offsetX < 0
        assert plan.operations[1].side == "left"

    def test_refuses_without_tracking(self):
        caps = {"capabilities": {**CAPS["capabilities"],
                                  "tracking": {"key": "tracking", "available": False,
                                               "reason": "no CSRT, no SAM 2"}}}
        with pytest.raises(CompileError) as exc:
            _plan(caps=caps)
        assert exc.value.code == "capability_unavailable"
        assert "SAM 2" in str(exc.value)

    def test_refuses_an_overlong_range(self):
        with pytest.raises(CompileError) as exc:
            _plan(params={**PARAMS, "range": {"start": 0.0, "end": 300.0}})
        assert exc.value.code == "range_too_long"


class TestMutations:
    def _ctx(self):
        return MutationContext(
            run_id=11, video_id=3, video_duration=30.0,
            staged_assets={
                "track": {"keyframes": TRACK_KEYFRAMES, "fps": 30.0},
                "label": {"generatedMediaId": 77,
                          "url": "https://cdn.example.test/label.png",
                          "width": 400, "height": 100},
            },
        )

    def test_places_the_label_and_binds_the_follow(self):
        plan = _plan()
        result = apply_plan({}, plan, self._ctx())
        item_id = entity_id(11, "place")
        item = next(i for i in result.draft["timelineMediaItems"] if i["id"] == item_id)
        assert item["kind"] == "image" and item["sourceKind"] == "generated"
        assert item["sourceId"] == 77 and item["audioEnabled"] is False
        keyframes = result.draft["clipAttributes"][f"media:{item_id}"]["keyframes"]
        xs = keyframes["video.x"]
        assert len(xs) == 3
        # offsetX applied, and the runaway third point clamped inside the frame.
        follow = next(op for op in plan.operations if op.id == "follow")
        assert xs[0]["v"] == pytest.approx(12.0 + follow.offsetX, abs=0.01)
        assert xs[2]["v"] <= 46.0
        assert keyframes["video.y"][0]["v"] == pytest.approx(-5.0, abs=0.01)

    def test_apply_then_revert_is_identity(self):
        plan = _plan()
        applied = apply_plan({"keepRanges": [{"id": "r1", "start": 0, "end": 30}]},
                             plan, self._ctx())
        reverted, warnings = revert_manifest(applied.draft, applied.inverse)
        assert warnings == []
        assert reverted == {"keepRanges": [{"id": "r1", "start": 0, "end": 30}]}

    def test_missing_staged_track_skips_the_follow_with_a_warning(self):
        plan = _plan()
        ctx = self._ctx()
        ctx.staged_assets.pop("track")
        result = apply_plan({}, plan, ctx)
        assert any("no keyframes" in w for w in result.warnings)


class TestLabelCard:
    def test_renders_a_real_png_both_sides(self):
        for side in ("left", "right"):
            data, width, height = render_label_card("New feature", side=side,
                                                    accent="#FFD400")
            assert data[:8] == b"\x89PNG\r\n\x1a\n"
            assert width > height > 0


class TestFullFlow:
    @pytest.fixture(autouse=True)
    def fakes(self, monkeypatch, db_session):
        monkeypatch.setattr(
            "app.api.routes.harness.enqueue_harness_apply_job", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "app.services.harness.executor.caps.snapshot", lambda: CAPS
        )
        monkeypatch.setattr(
            executor_module, "_run_tracking",
            lambda source, **kwargs: {
                "keyframes": TRACK_KEYFRAMES, "fps": 30.0, "lostAtFrame": 120,
            },
        )
        monkeypatch.setattr(
            executor_module, "_render_label", lambda *a, **k: (b"\x89PNG\r\n\x1a\nfake", 400, 100)
        )

        class _FakeStorage:
            def upload_bytes(self, data, *, key, content_type):
                from app.storage.base import UploadResult

                return UploadResult(
                    url=f"https://cdn.example.test/{key}", bytes=len(data),
                    key=key, content_type=content_type,
                )

        import app.storage as storage_module

        monkeypatch.setattr(storage_module, "storage_available", lambda: True)
        monkeypatch.setattr(storage_module, "get_storage", lambda name=None: _FakeStorage())

    def test_plan_apply_verify_with_lost_tracking_warning(
        self, db_session, api_client, make_user, make_project, make_video
    ):
        from app.db.models import GeneratedMedia
        from app.services import draft_store

        user = make_user()
        project = make_project(creator=user)
        video = make_video(project=project, duration=30)
        db_session.commit()
        api_client.login(user)

        run = api_client.post(
            f"/videos/{video.id}/editing/runs",
            json={"recipe_id": "tracked_callout", "params": PARAMS},
        ).json()
        assert run["state"] == "planned", run.get("error_detail")
        assert len(run["operations"]) == 4

        api_client.post(
            f"/editing/runs/{run['id']}/approve",
            json={"plan_checksum": run["plan_checksum"]},
        )
        applied = api_client.post(f"/editing/runs/{run['id']}/apply", json={}).json()
        assert applied["state"] == "ready", applied.get("error_detail")
        assert applied["verification_report"]["status"] in {"pass", "warnings"}
        assert any("lost the object" in w for w in applied["warnings"])

        view = draft_store.get_draft(db_session, project.id)
        item_id = entity_id(run["id"], "place")
        items = {i["id"] for i in view.payload["timelineMediaItems"]}
        assert item_id in items
        assert view.payload["clipAttributes"][f"media:{item_id}"]["keyframes"]["video.x"]

        # The label asset is a real, owned GeneratedMedia row.
        asset = db_session.query(GeneratedMedia).one()
        assert asset.model == "harness/label-card" and asset.status == "ready"

        reverted = api_client.post(f"/editing/runs/{run['id']}/revert").json()
        assert reverted["state"] == "reverted"
        after = draft_store.get_draft(db_session, project.id)
        assert item_id not in {
            i.get("id") for i in after.payload.get("timelineMediaItems", [])
        }
        # Reverting the run never deletes the paid-for/rendered asset.
        assert db_session.query(GeneratedMedia).count() == 1
