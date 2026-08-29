"""Natural-language intent → harness plan: the provider chain and the run path."""

from __future__ import annotations

import pytest

from app.services.harness import planner as planner_module
from app.services.harness.planner import PlannerError, PlannerResult, plan_recipe_params

SEG_SNAPSHOT = {
    "capabilities": {
        "segmentation": {
            "key": "segmentation",
            "available": True,
            "limits": {"maxClipSeconds": 120},
            "detail": {"autoMatte": True},
        }
    }
}

PLANNED = PlannerResult(
    recipe_id="subject_behind_text",
    params={"range": {"start": 2.0, "end": 8.0}, "text": "BIG IDEA"},
    model="claude-opus-5",
    usage={"input_tokens": 100, "output_tokens": 50},
)


class TestProviderChain:
    def test_claude_wins_when_available(self, monkeypatch):
        monkeypatch.setattr(
            planner_module, "planner_availability",
            lambda: {"claude": True, "openrouter": True},
        )
        monkeypatch.setattr(planner_module, "plan_with_claude", lambda *a, **k: PLANNED)
        monkeypatch.setattr(
            planner_module, "plan_subject_behind_text",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("openrouter called")),
        )
        result = plan_recipe_params("put me behind a title", video_duration=30)
        assert result.model == "claude-opus-5"

    def test_claude_failure_falls_through_to_openrouter_loudly(self, monkeypatch):
        monkeypatch.setattr(
            planner_module, "planner_availability",
            lambda: {"claude": True, "openrouter": True},
        )
        monkeypatch.setattr(
            planner_module, "plan_with_claude",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("refused")),
        )
        fallback = PlannerResult(
            recipe_id="subject_behind_text", params=PLANNED.params,
            model="deepseek/deepseek-chat:free", usage={},
        )
        monkeypatch.setattr(
            planner_module, "plan_subject_behind_text", lambda *a, **k: fallback
        )
        result = plan_recipe_params("x", video_duration=30)
        assert result.model.endswith(":free")

    def test_no_provider_names_what_to_configure(self, monkeypatch):
        monkeypatch.setattr(
            planner_module, "planner_availability",
            lambda: {"claude": False, "openrouter": False},
        )
        with pytest.raises(PlannerError) as exc:
            plan_recipe_params("x", video_duration=30)
        assert "ANTHROPIC_API_KEY" in str(exc.value)
        assert "OPENROUTER_API_KEY" in str(exc.value)

    def test_both_failing_reports_both_errors(self, monkeypatch):
        monkeypatch.setattr(
            planner_module, "planner_availability",
            lambda: {"claude": True, "openrouter": True},
        )
        monkeypatch.setattr(
            planner_module, "plan_with_claude",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("model overloaded")),
        )
        monkeypatch.setattr(
            planner_module, "plan_subject_behind_text",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("all free models 429")),
        )
        with pytest.raises(PlannerError) as exc:
            plan_recipe_params("x", video_duration=30)
        message = str(exc.value)
        assert "claude: model overloaded" in message
        assert "openrouter: all free models 429" in message


class TestIntentRuns:
    @pytest.fixture(autouse=True)
    def never_enqueue(self, monkeypatch):
        monkeypatch.setattr(
            "app.api.routes.harness.enqueue_harness_apply_job", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "app.services.harness.executor.caps.snapshot", lambda: SEG_SNAPSHOT
        )

    @pytest.fixture
    def editor(self, db_session, api_client, make_user, make_project, make_video):
        user = make_user()
        project = make_project(creator=user)
        video = make_video(project=project, duration=30)
        db_session.commit()
        api_client.login(user)
        return api_client, video

    def test_intent_only_plans_via_the_model_and_records_provenance(
        self, editor, monkeypatch
    ):
        client, video = editor
        seen: dict[str, object] = {}

        def _fake_plan(intent, **kwargs):
            seen["intent"] = intent
            seen.update(kwargs)
            return PLANNED

        monkeypatch.setattr(
            "app.services.harness.planner.plan_recipe_params", _fake_plan
        )
        response = client.post(
            f"/videos/{video.id}/editing/runs",
            json={
                "recipe_id": "subject_behind_text",
                "intent": "Put me behind a big title that says BIG IDEA",
                "selection": {"start": 2.0, "end": 8.0},
            },
        )
        body = response.json()
        assert body["state"] == "planned", body.get("error_detail")
        assert seen["selection"] == {"start": 2.0, "end": 8.0}
        assert seen["max_clip_seconds"] == 120
        # Provenance: which model answered, at what cost.
        assert body["params"]["text"] == "BIG IDEA"
        ops = {op["type"] for op in body["operations"]}
        assert "overlay.create_text" in ops

    def test_no_planner_lands_in_needs_input_with_the_remedy(
        self, editor, monkeypatch
    ):
        client, video = editor
        monkeypatch.setattr(
            "app.services.harness.planner.plan_recipe_params",
            lambda *a, **k: (_ for _ in ()).throw(
                PlannerError("No planning model is configured. Set ANTHROPIC_API_KEY …")
            ),
        )
        response = client.post(
            f"/videos/{video.id}/editing/runs",
            json={"recipe_id": "subject_behind_text", "intent": "do the thing"},
        )
        body = response.json()
        assert body["state"] == "needs_input"
        assert body["error_code"] == "planner_unavailable"
        assert "ANTHROPIC_API_KEY" in body["error_detail"]

    def test_explicit_params_never_touch_the_planner(self, editor, monkeypatch):
        client, video = editor
        monkeypatch.setattr(
            "app.services.harness.planner.plan_recipe_params",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("planner called")),
        )
        response = client.post(
            f"/videos/{video.id}/editing/runs",
            json={
                "recipe_id": "subject_behind_text",
                "params": {"range": {"start": 1.0, "end": 5.0}, "text": "T"},
                "intent": "also described, but params win",
            },
        )
        assert response.json()["state"] == "planned"

    def test_model_proposed_params_still_hit_compile_validation(
        self, editor, monkeypatch
    ):
        client, video = editor
        bad = PlannerResult(
            recipe_id="subject_behind_text",
            params={"range": {"start": 0.0, "end": 500.0}, "text": "TOO LONG"},
            model="claude-opus-5",
            usage={},
        )
        monkeypatch.setattr(
            "app.services.harness.planner.plan_recipe_params", lambda *a, **k: bad
        )
        response = client.post(
            f"/videos/{video.id}/editing/runs",
            json={"recipe_id": "subject_behind_text", "intent": "everything"},
        )
        body = response.json()
        assert body["state"] == "failed"
        assert body["error_code"] == "range_too_long"


class TestPlannerProbe:
    def test_probe_reports_providers(self, monkeypatch):
        from app.services.harness.capabilities import probe_planner

        monkeypatch.setattr(
            "app.services.harness.planner.planner_availability",
            lambda: {"claude": False, "openrouter": True},
        )
        entry = probe_planner()
        assert entry["available"] is True
        assert entry["provider"] == "openrouter"

        monkeypatch.setattr(
            "app.services.harness.planner.planner_availability",
            lambda: {"claude": False, "openrouter": False},
        )
        entry = probe_planner()
        assert entry["available"] is False
        assert "ANTHROPIC_API_KEY" in entry["reason"]
