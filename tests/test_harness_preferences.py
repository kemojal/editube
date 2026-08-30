"""Preference learning (plan Phase 5) — deterministic, inspectable, resettable.

Nothing is trained: every learned value is a reproducible query over the
user's own run history. These tests pin the three contracts: style defaults
come from kept runs only (majority, whitelist, never content keys), the
measured revert rate gates auto-apply, and inspect/disable/reset behave as
declared — reset by cutoff, disable by flag, runs untouched.
"""

from __future__ import annotations

import pytest

from app.db.models import HarnessRun
from app.services.harness import preferences


@pytest.fixture(autouse=True)
def no_real_queue(monkeypatch):
    monkeypatch.setattr(
        "app.api.routes.harness.enqueue_harness_apply_job", lambda *a, **k: None
    )


@pytest.fixture()
def seeded(db_session, api_client, make_user, make_project, make_video):
    user = make_user()
    project = make_project(creator=user)
    video = make_video(project=project, duration=20)
    db_session.commit()
    api_client.login(user)
    api_client.put(
        f"/videos/{video.id}/ai/rough-cut-draft",
        json={"keepRanges": [{"id": "r1", "start": 0.0, "end": 12.0}]},
    )
    return user, project, video


def _settled_run(db, user, project, video, *, state, params=None, recipe="subject_behind_text"):
    run = HarnessRun(
        project_id=project.id,
        video_id=video.id,
        created_by=user.id,
        state=state,
        recipe_id=recipe,
        params=params or {},
    )
    db.add(run)
    db.commit()
    return run


class TestLearnedDefaults:
    def test_majority_of_kept_runs_wins(self, db_session, seeded):
        user, project, video = seeded
        for _ in range(2):
            _settled_run(db_session, user, project, video, state="ready",
                         params={"dimBackground": False, "templateId": "bold"})
        for _ in range(3):
            _settled_run(db_session, user, project, video, state="ready",
                         params={"dimBackground": True, "templateId": "minimal"})
        defaults = preferences.learned_defaults(db_session, user.id, "subject_behind_text")
        assert defaults == {"dimBackground": True, "templateId": "minimal"}

    def test_reverted_runs_teach_nothing(self, db_session, seeded):
        user, project, video = seeded
        _settled_run(db_session, user, project, video, state="reverted",
                     params={"templateId": "bold"})
        assert preferences.learned_defaults(db_session, user.id, "subject_behind_text") == {}

    def test_content_keys_are_never_learned(self, db_session, seeded):
        user, project, video = seeded
        _settled_run(db_session, user, project, video, state="ready",
                     params={"text": "MY TITLE", "range": {"start": 0, "end": 5},
                             "templateId": "minimal"})
        defaults = preferences.learned_defaults(db_session, user.id, "subject_behind_text")
        assert "text" not in defaults and "range" not in defaults
        assert defaults["templateId"] == "minimal"

    def test_learned_defaults_fill_unset_keys_at_planning(
        self, db_session, api_client, seeded, monkeypatch
    ):
        user, project, video = seeded
        for _ in range(3):
            _settled_run(db_session, user, project, video, state="ready",
                         params={"dimBackground": False})
        run = api_client.post(
            f"/videos/{video.id}/editing/runs",
            json={
                "recipe_id": "subject_behind_text",
                "params": {"range": {"start": 1.0, "end": 5.0}, "text": "HI"},
            },
        ).json()
        # dimBackground learned False -> no dim operation in the plan.
        assert run["params"]["dimBackground"] is False

    def test_explicit_values_beat_learned_ones(self, db_session, api_client, seeded):
        user, project, video = seeded
        for _ in range(3):
            _settled_run(db_session, user, project, video, state="ready",
                         params={"dimBackground": False})
        run = api_client.post(
            f"/videos/{video.id}/editing/runs",
            json={
                "recipe_id": "subject_behind_text",
                "params": {
                    "range": {"start": 1.0, "end": 5.0},
                    "text": "HI",
                    "dimBackground": True,
                },
            },
        ).json()
        assert run["params"]["dimBackground"] is True

    def test_disabling_learning_stops_the_fill(self, db_session, api_client, seeded):
        user, project, video = seeded
        for _ in range(3):
            _settled_run(db_session, user, project, video, state="ready",
                         params={"dimBackground": False})
        preferences.set_learning_enabled(db_session, user.id, False)
        run = api_client.post(
            f"/videos/{video.id}/editing/runs",
            json={
                "recipe_id": "subject_behind_text",
                "params": {"range": {"start": 1.0, "end": 5.0}, "text": "HI"},
            },
        ).json()
        assert "dimBackground" not in run["params"]

    def test_reset_forgets_by_cutoff_and_keeps_the_runs(self, db_session, seeded):
        user, project, video = seeded
        _settled_run(db_session, user, project, video, state="ready",
                     params={"templateId": "bold"})
        assert preferences.learned_defaults(db_session, user.id, "subject_behind_text")
        preferences.reset(db_session, user.id)
        assert preferences.learned_defaults(db_session, user.id, "subject_behind_text") == {}
        assert db_session.query(HarnessRun).count() == 1


class TestRevertRateGate:
    def test_a_high_revert_rate_closes_the_gate(self, db_session, seeded):
        user, project, video = seeded
        for _ in range(2):
            _settled_run(db_session, user, project, video, state="reverted",
                         recipe="review_fix")
        _settled_run(db_session, user, project, video, state="ready", recipe="review_fix")
        reason = preferences.auto_apply_gate(db_session, user.id, "review_fix")
        assert reason is not None and "back off" in reason

    def test_too_small_a_sample_keeps_the_gate_open(self, db_session, seeded):
        user, project, video = seeded
        _settled_run(db_session, user, project, video, state="reverted", recipe="review_fix")
        assert preferences.auto_apply_gate(db_session, user.id, "review_fix") is None

    def test_the_gate_declines_a_real_auto_apply(self, db_session, api_client, seeded):
        user, project, video = seeded
        for _ in range(2):
            _settled_run(db_session, user, project, video, state="reverted",
                         recipe="review_fix")
        _settled_run(db_session, user, project, video, state="ready", recipe="review_fix")
        grant = api_client.post(
            f"/videos/{video.id}/editing/auto_apply_grants",
            json={"recipe_id": "review_fix"},
        ).json()
        run = api_client.post(
            f"/videos/{video.id}/editing/runs",
            json={
                "recipe_id": "review_fix",
                "params": {"range": {"start": 1.0, "end": 6.0},
                           "adjust": {"exposure": -10.0}},
                "auto_apply_grant_id": grant["id"],
            },
        ).json()
        assert run["state"] == "planned"
        assert run["auto_applied"] is False
        assert any("back off" in w for w in run["warnings"])


class TestInspectEndpoints:
    def test_the_snapshot_shows_stats_defaults_and_governance(
        self, db_session, api_client, seeded
    ):
        user, project, video = seeded
        _settled_run(db_session, user, project, video, state="ready",
                     params={"templateId": "minimal"})
        body = api_client.get("/editing/preferences").json()
        assert body["learningEnabled"] is True
        recipe = body["recipes"]["subject_behind_text"]
        assert recipe["stats"]["kept"] == 1
        assert recipe["learnedDefaults"]["templateId"] == "minimal"
        assert "never from media" in body["governance"]

    def test_patch_and_reset_round_trip(self, api_client, seeded):
        body = api_client.patch(
            "/editing/preferences", json={"learning_enabled": False}
        ).json()
        assert body["learningEnabled"] is False
        body = api_client.post("/editing/preferences/reset").json()
        assert body["resetAt"] is not None
