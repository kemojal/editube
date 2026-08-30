"""Controlled autonomy (plan Phase 5): one-shot auto-apply grants.

The consent model is the auto-edit gate's, transplanted: the user grants
once, the server spends the grant exactly once, and a replayed request finds
it already spent. Auto-apply is limited to reversible operations, and an
auto-applied run that fails verification is reverted automatically — nobody
reviewed it, so nothing unverified may stay on the timeline.
"""

from __future__ import annotations

import pytest

from app.db.models import HarnessAutoApplyGrant
from app.services.harness import executor as executor_module

FIX_PARAMS = {
    "range": {"start": 1.0, "end": 6.0},
    "adjust": {"exposure": -10.0},
    "note": "too bright",
}


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


def _grant(api_client, video, recipe="review_fix"):
    response = api_client.post(
        f"/videos/{video.id}/editing/auto_apply_grants", json={"recipe_id": recipe}
    )
    assert response.status_code == 200, response.text
    return response.json()


class TestAutoApply:
    def test_a_qualifying_run_applies_with_no_approve_click(
        self, db_session, api_client, seeded
    ):
        _, project, video = seeded
        grant = _grant(api_client, video)
        run = api_client.post(
            f"/videos/{video.id}/editing/runs",
            json={
                "recipe_id": "review_fix",
                "params": FIX_PARAMS,
                "auto_apply_grant_id": grant["id"],
            },
        ).json()
        assert run["state"] == "ready", run.get("error_detail")
        assert run["auto_applied"] is True
        # The grant is spent, and names the run it was spent on.
        row = db_session.query(HarnessAutoApplyGrant).one()
        assert row.spent_at is not None and row.spent_run_id == run["id"]
        # The change actually landed.
        from app.services import draft_store

        view = draft_store.get_draft(db_session, project.id)
        assert view.payload["clipAttributes"]["video:r1"]["adjust"]["exposure"] == -10.0

    def test_a_spent_grant_cannot_apply_twice(self, db_session, api_client, seeded):
        _, _, video = seeded
        grant = _grant(api_client, video)
        first = api_client.post(
            f"/videos/{video.id}/editing/runs",
            json={
                "recipe_id": "review_fix",
                "params": FIX_PARAMS,
                "auto_apply_grant_id": grant["id"],
            },
        ).json()
        assert first["state"] == "ready"

        second = api_client.post(
            f"/videos/{video.id}/editing/runs",
            json={
                "recipe_id": "review_fix",
                "params": FIX_PARAMS,
                "auto_apply_grant_id": grant["id"],
            },
        ).json()
        # The replay plans normally but does NOT apply; the warning says why.
        assert second["state"] == "planned"
        assert second["auto_applied"] is False
        assert any("already used" in w for w in second["warnings"])

    def test_a_mismatched_recipe_leaves_the_grant_unspent(
        self, db_session, api_client, seeded
    ):
        _, _, video = seeded
        grant = _grant(api_client, video, recipe="subject_behind_text")
        run = api_client.post(
            f"/videos/{video.id}/editing/runs",
            json={
                "recipe_id": "review_fix",
                "params": FIX_PARAMS,
                "auto_apply_grant_id": grant["id"],
            },
        ).json()
        assert run["state"] == "planned"
        assert any("different recipe" in w for w in run["warnings"])
        assert db_session.query(HarnessAutoApplyGrant).one().spent_at is None

    def test_a_plan_that_needs_input_leaves_the_grant_unspent(
        self, db_session, api_client, seeded, monkeypatch
    ):
        _, _, video = seeded
        # Force the planner chain down: an intent-only run lands needs_input.
        from app.services.harness.planner import PlannerError

        def _no_planner(*args, **kwargs):
            raise PlannerError("no planner in tests")

        monkeypatch.setattr(
            "app.services.harness.planner.plan_recipe_params", _no_planner
        )
        grant = _grant(api_client, video)
        run = api_client.post(
            f"/videos/{video.id}/editing/runs",
            json={
                "recipe_id": "review_fix",
                "params": {},
                "intent": "fix the brightness somewhere",
                "auto_apply_grant_id": grant["id"],
            },
        ).json()
        assert run["state"] != "ready"
        assert run["auto_applied"] is False
        assert db_session.query(HarnessAutoApplyGrant).one().spent_at is None

    def test_someone_elses_grant_is_not_yours_to_spend(
        self, db_session, api_client, seeded, make_user
    ):
        user, _, video = seeded
        grant = _grant(api_client, video)
        api_client.logout()
        other = make_user()
        db_session.commit()
        api_client.login(other)
        response = api_client.post(
            f"/videos/{video.id}/editing/runs",
            json={
                "recipe_id": "review_fix",
                "params": FIX_PARAMS,
                "auto_apply_grant_id": grant["id"],
            },
        )
        # Either the project denies the write outright or the grant lookup
        # (scoped to the caller) misses — never a spend of someone else's
        # consent.
        assert response.status_code in {403, 404}
        assert db_session.query(HarnessAutoApplyGrant).one().spent_at is None

    def test_failed_verification_reverts_automatically(
        self, db_session, api_client, seeded, monkeypatch
    ):
        _, project, video = seeded
        monkeypatch.setattr(
            executor_module,
            "verify_committed",
            lambda payload, run, plan: {
                "status": "fail",
                "checks": [{"check": "structure", "status": "fail", "detail": "broken"}],
            },
        )
        grant = _grant(api_client, video)
        run = api_client.post(
            f"/videos/{video.id}/editing/runs",
            json={
                "recipe_id": "review_fix",
                "params": FIX_PARAMS,
                "auto_apply_grant_id": grant["id"],
            },
        ).json()
        assert run["state"] == "reverted"
        assert "automatically" in (run["stage"] or "")
        assert any("reverted automatically" in w for w in run["warnings"])
        # Nothing unverified stayed on the draft.
        from app.services import draft_store

        view = draft_store.get_draft(db_session, project.id)
        adjust = (view.payload.get("clipAttributes", {}).get("video:r1") or {}).get("adjust")
        assert not adjust

    def test_granting_an_unknown_recipe_is_a_404(self, api_client, seeded):
        _, _, video = seeded
        response = api_client.post(
            f"/videos/{video.id}/editing/auto_apply_grants",
            json={"recipe_id": "no_such_recipe"},
        )
        assert response.status_code == 404
