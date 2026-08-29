"""AI-Review finding → harness plan: the machine block and the recipe."""

from __future__ import annotations

import pytest

from app.services.harness.compiler import CompileError, compile_recipe
from app.services.video_review import _harden_fix_action, _harden_notes

DRAFT = {"keepRanges": [{"id": "r1", "start": 0.0, "end": 12.0}]}
SNAPSHOT = {"capabilities": {}}


class TestHardenFixAction:
    def test_clamps_and_allowlists_adjust(self):
        action = _harden_fix_action(
            {"adjust": {"exposure": 900, "lut": 5, "saturation": "-15"}}
        )
        assert action == {"adjust": {"exposure": 100.0, "saturation": -15.0}}

    def test_clamps_audio_bounds(self):
        action = _harden_fix_action({"audio": {"volume": -200, "fadeIn": 99, "junk": 1}})
        assert action == {"audio": {"volume": -60.0, "fadeIn": 10.0}}

    def test_garbage_becomes_none(self):
        assert _harden_fix_action("louder please") is None
        assert _harden_fix_action({"adjust": {"lut": 3}}) is None

    def test_notes_only_carry_actions_for_actionable_categories(self):
        notes = _harden_notes(
            [
                {"text": "too dark", "category": "visuals", "start": 1,
                 "fixAction": {"adjust": {"exposure": 20}}},
                {"text": "weak hook", "category": "hook", "start": 0,
                 "fixAction": {"adjust": {"exposure": 20}}},
            ],
            frames=[],
        )
        assert notes[0]["fixAction"] == {"adjust": {"exposure": 20.0}}
        assert "fixAction" not in notes[1]


class TestReviewFixRecipe:
    def _compile(self, params, draft=DRAFT):
        return compile_recipe(
            "review_fix", params,
            capability_snapshot=SNAPSHOT, video_duration=20.0, draft=draft,
        )

    def test_compiles_adjust_and_audio_onto_the_flagged_clip(self):
        plan = self._compile(
            {
                "range": {"start": 2.0, "end": 5.0},
                "adjust": {"exposure": 18.0},
                "audio": {"volume": 4.0},
                "note": "underexposed and quiet",
            }
        )
        assert [op.type for op in plan.operations] == ["visual.adjust", "audio.adjust"]
        for op in plan.operations:
            assert op.clipKey == "video:r1"
            assert op.fingerprint.start == 0.0 and op.fingerprint.end == 12.0

    def test_requires_the_recipe_to_do_something(self):
        with pytest.raises(CompileError) as exc:
            self._compile({"range": {"start": 2.0, "end": 5.0}})
        assert exc.value.code == "empty_fix"

    def test_unaddressable_clip_fails_with_the_remedy(self):
        with pytest.raises(CompileError) as exc:
            self._compile(
                {"range": {"start": 2.0, "end": 5.0}, "adjust": {"exposure": 10.0}},
                draft={"keepRanges": [{"start": 0.0, "end": 12.0}]},  # no id
            )
        assert exc.value.code == "clip_not_found"
        assert "save once" in str(exc.value)

    def test_needs_no_special_capabilities(self):
        from app.services.harness.compiler import list_recipes

        recipes = {r["id"]: r for r in list_recipes(SNAPSHOT)}
        assert recipes["review_fix"]["available"] is True


class TestRouteFlow:
    @pytest.fixture(autouse=True)
    def no_queue(self, monkeypatch):
        monkeypatch.setattr(
            "app.api.routes.harness.enqueue_harness_apply_job", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "app.services.harness.executor.caps.snapshot", lambda: SNAPSHOT
        )

    def test_review_fix_plans_and_applies_with_exact_revert(
        self, db_session, api_client, make_user, make_project, make_video
    ):
        from app.services import draft_store

        user = make_user()
        project = make_project(creator=user)
        video = make_video(project=project, duration=20)
        db_session.commit()
        api_client.login(user)

        api_client.put(
            f"/videos/{video.id}/ai/rough-cut-draft",
            json={
                "keepRanges": [{"id": "r1", "start": 0.0, "end": 12.0}],
                "clipAttributes": {"video:r1": {"adjust": {"vignette": 4.0}}},
                "rangeEditVersion": 3,
            },
        )
        before = draft_store.get_draft(db_session, project.id)

        run = api_client.post(
            f"/videos/{video.id}/editing/runs",
            json={
                "recipe_id": "review_fix",
                "params": {
                    "range": {"start": 2.0, "end": 5.0},
                    "adjust": {"exposure": 18.0},
                },
            },
        ).json()
        assert run["state"] == "planned", run.get("error_detail")

        api_client.post(
            f"/editing/runs/{run['id']}/approve",
            json={"plan_checksum": run["plan_checksum"]},
        )
        applied = api_client.post(f"/editing/runs/{run['id']}/apply", json={}).json()
        assert applied["state"] == "ready"

        after = draft_store.get_draft(db_session, project.id)
        merged = after.payload["clipAttributes"]["video:r1"]["adjust"]
        assert merged == {"vignette": 4.0, "exposure": 18.0}

        api_client.post(f"/editing/runs/{run['id']}/revert")
        reverted = draft_store.get_draft(db_session, project.id)
        assert (
            reverted.payload["clipAttributes"]["video:r1"]["adjust"]
            == before.payload["clipAttributes"]["video:r1"]["adjust"]
        )
