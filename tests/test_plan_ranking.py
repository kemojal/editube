"""Plan ranking and deterministic fallbacks (plan Phase 5).

A model proposal that fails to compile gets mechanical repairs — re-anchor
to the user's selection, clamp the range — before the run gives up, and when
several candidates compile a fixed score picks one. Everything here is
deterministic and the run records the trail, so these tests can pin exact
choices, not tendencies.
"""

from __future__ import annotations

import pytest

from app.services.harness import executor as executor_module
from app.services.harness.planner import PlannerResult
from app.services.harness.ranking import candidate_variants, choose_candidate

CAPS = {
    "capabilities": {
        "segmentation": {
            "key": "segmentation",
            "available": True,
            "limits": {"maxClipSeconds": 120},
            "detail": {"autoMatte": True},
        },
    }
}

GOOD = {"range": {"start": 1.0, "end": 5.0}, "text": "HELLO"}


class TestCandidateVariants:
    def test_the_model_proposal_always_comes_first(self):
        variants = candidate_variants(
            GOOD, selection=None, video_duration=30, max_clip_seconds=120
        )
        assert variants[0] == ("model", GOOD)

    def test_selection_and_clamp_repairs_follow_in_fixed_order(self):
        variants = candidate_variants(
            {"range": {"start": 10.0, "end": 99.0}, "text": "HI"},
            selection={"start": 2.0, "end": 6.0},
            video_duration=30,
            max_clip_seconds=120,
        )
        assert [label for label, _ in variants] == [
            "model", "selection-range", "clamped-range",
        ]
        assert variants[1][1]["range"] == {"start": 2.0, "end": 6.0}
        assert variants[2][1]["range"] == {"start": 10.0, "end": 30.0}
        # Repairs never touch anything but the range.
        assert variants[2][1]["text"] == "HI"

    def test_a_repair_identical_to_the_proposal_is_deduplicated(self):
        variants = candidate_variants(
            GOOD, selection={"start": 1.0, "end": 5.0}, video_duration=30,
            max_clip_seconds=120,
        )
        # Selection == the proposal's range and the clamp of an in-bounds
        # range IS the range: every repair collapses into the proposal.
        assert [label for label, _ in variants] == ["model"]

    def test_an_over_cap_span_is_clamped_to_the_cap(self):
        variants = candidate_variants(
            {"range": {"start": 0.0, "end": 500.0}, "text": "HI"},
            selection=None,
            video_duration=600,
            max_clip_seconds=120,
        )
        clamped = dict(variants)["clamped-range"]
        assert clamped["range"] == {"start": 0.0, "end": 120.0}


class TestChooseCandidate:
    def test_a_compiling_proposal_wins_unrepaired(self):
        variants = candidate_variants(
            GOOD, selection={"start": 2.0, "end": 6.0}, video_duration=30,
            max_clip_seconds=120,
        )
        params, plan, report = choose_candidate(
            "subject_behind_text", variants,
            capability_snapshot=CAPS, video_duration=30, draft=None,
        )
        assert report["chosen"] == "model"
        assert params == GOOD and plan is not None

    def test_a_broken_proposal_falls_back_to_its_repair(self):
        variants = candidate_variants(
            {"range": {"start": 10.0, "end": 99.0}, "text": "HI"},
            selection=None, video_duration=30, max_clip_seconds=120,
        )
        params, plan, report = choose_candidate(
            "subject_behind_text", variants,
            capability_snapshot=CAPS, video_duration=30, draft=None,
        )
        assert report["chosen"] == "clamped-range"
        assert params["range"] == {"start": 10.0, "end": 30.0}
        assert report["considered"][0] == {
            "candidate": "model", "outcome": "range_outside_media",
        }

    def test_learned_agreement_breaks_a_warnings_tie(self):
        variants = [
            ("model", {**GOOD, "templateId": "minimal"}),
            ("alt", {**GOOD, "templateId": "glass"}),
        ]
        params, _, report = choose_candidate(
            "subject_behind_text", variants,
            capability_snapshot=CAPS, video_duration=30, draft=None,
            learned={"templateId": "glass"},
        )
        assert report["chosen"] == "alt"
        assert params["templateId"] == "glass"

    def test_garbage_ranks_as_a_failed_candidate_not_a_crash(self):
        params, plan, report = choose_candidate(
            "subject_behind_text",
            [("model", {"nonsense": True})],
            capability_snapshot=CAPS, video_duration=30, draft=None,
        )
        assert params is None and plan is None
        assert report["considered"] == [
            {"candidate": "model", "outcome": "invalid_params"}
        ]


class TestThroughTheRun:
    @pytest.fixture(autouse=True)
    def fakes(self, monkeypatch):
        monkeypatch.setattr(
            "app.api.routes.harness.enqueue_harness_apply_job", lambda *a, **k: None
        )
        monkeypatch.setattr(executor_module.caps, "snapshot", lambda: CAPS)

    @pytest.fixture()
    def seeded(self, db_session, api_client, make_user, make_project, make_video):
        user = make_user()
        project = make_project(creator=user)
        video = make_video(project=project, duration=30)
        db_session.commit()
        api_client.login(user)
        return user, project, video

    def _planner_returns(self, monkeypatch, params):
        monkeypatch.setattr(
            "app.services.harness.planner.plan_recipe_params",
            lambda *a, **k: PlannerResult(
                recipe_id="subject_behind_text", params=params,
                model="test/model", usage={},
            ),
        )

    def test_a_repaired_intent_run_plans_with_the_trail_recorded(
        self, api_client, seeded, monkeypatch
    ):
        _, _, video = seeded
        self._planner_returns(
            monkeypatch, {"range": {"start": 10.0, "end": 99.0}, "text": "LAUNCH"}
        )
        run = api_client.post(
            f"/videos/{video.id}/editing/runs",
            json={"recipe_id": "subject_behind_text", "intent": "title over the end"},
        ).json()
        assert run["state"] == "planned", run.get("error_detail")
        assert run["params"]["range"] == {"start": 10.0, "end": 30.0}
        assert any("repair" in w for w in run["warnings"])
        assert run["diff"]["planner"]["chosen"] == "clamped-range"
        assert run["diff"]["planner"]["considered"][0]["outcome"] == "range_outside_media"

    def test_an_unrepairable_intent_run_fails_with_the_models_own_error(
        self, api_client, seeded, monkeypatch
    ):
        _, _, video = seeded
        self._planner_returns(monkeypatch, {"nonsense": True})
        run = api_client.post(
            f"/videos/{video.id}/editing/runs",
            json={"recipe_id": "subject_behind_text", "intent": "do something odd"},
        ).json()
        assert run["state"] == "failed"
        assert run["error_code"] == "invalid_params"

    def test_a_clean_intent_run_records_the_model_as_chosen(
        self, api_client, seeded, monkeypatch
    ):
        _, _, video = seeded
        self._planner_returns(monkeypatch, dict(GOOD))
        run = api_client.post(
            f"/videos/{video.id}/editing/runs",
            json={"recipe_id": "subject_behind_text", "intent": "a title"},
        ).json()
        assert run["state"] == "planned"
        assert run["diff"]["planner"]["chosen"] == "model"
        assert not any("repair" in w for w in run["warnings"])

    def test_explicit_params_are_never_silently_repaired(
        self, api_client, seeded, monkeypatch
    ):
        _, _, video = seeded
        run = api_client.post(
            f"/videos/{video.id}/editing/runs",
            json={
                "recipe_id": "subject_behind_text",
                "params": {"range": {"start": 10.0, "end": 99.0}, "text": "HI"},
            },
        ).json()
        assert run["state"] == "failed"
        assert run["error_code"] == "range_outside_media"


class TestConversationMemory:
    """The planner sees this video's earlier exchanges (chat memory)."""

    @pytest.fixture(autouse=True)
    def fakes(self, monkeypatch):
        monkeypatch.setattr(
            "app.api.routes.harness.enqueue_harness_apply_job", lambda *a, **k: None
        )
        monkeypatch.setattr(executor_module.caps, "snapshot", lambda: CAPS)

    def test_prior_runs_reach_the_planner_oldest_first(
        self, db_session, api_client, make_user, make_project, make_video, monkeypatch
    ):
        from app.db.models import HarnessRun

        user = make_user()
        project = make_project(creator=user)
        video = make_video(project=project, duration=30)
        db_session.commit()
        api_client.login(user)

        for intent, state in [("first title", "reverted"), ("second title", "ready")]:
            db_session.add(
                HarnessRun(
                    project_id=project.id, video_id=video.id, created_by=user.id,
                    state=state, recipe_id="subject_behind_text", intent=intent,
                )
            )
        db_session.commit()

        captured: dict = {}

        def _capture(intent, **kwargs):
            captured.update(kwargs, intent=intent)
            return PlannerResult(
                recipe_id="subject_behind_text", params=dict(GOOD),
                model="test/model", usage={},
            )

        monkeypatch.setattr(
            "app.services.harness.planner.plan_recipe_params", _capture
        )
        run = api_client.post(
            f"/videos/{video.id}/editing/runs",
            json={"recipe_id": "subject_behind_text", "intent": "make it bigger"},
        ).json()
        assert run["state"] == "planned"
        history = captured["history"]
        assert [item["intent"] for item in history] == ["first title", "second title"]
        # Outcomes ride along — a reverted run is a preference signal.
        assert history[0]["state"] == "reverted"

    def test_the_runs_list_carries_the_words(
        self, db_session, api_client, make_user, make_project, make_video
    ):
        from app.db.models import HarnessRun

        user = make_user()
        project = make_project(creator=user)
        video = make_video(project=project, duration=30)
        db_session.commit()
        api_client.login(user)
        db_session.add(
            HarnessRun(
                project_id=project.id, video_id=video.id, created_by=user.id,
                state="ready", recipe_id="subject_behind_text", intent="hello title",
            )
        )
        db_session.commit()
        listed = api_client.get(f"/videos/{video.id}/editing/runs").json()
        assert listed["runs"][0]["intent"] == "hello title"
