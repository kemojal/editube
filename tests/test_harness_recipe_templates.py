"""Team recipe templates (plan Phases 4/5): workspace house style.

Same whitelist as preference learning — style keys only, never content —
and a fixed precedence at planning time: explicit params > the user's own
learned defaults > the team template > the recipe's built-ins, each layer
filling only what the layer above left unset.
"""

from __future__ import annotations

import pytest

from app.db.models import HarnessRecipeTemplate, HarnessRun
from app.services.harness import executor as executor_module
from app.services.harness import preferences

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


@pytest.fixture(autouse=True)
def no_real_queue(monkeypatch):
    monkeypatch.setattr(
        "app.api.routes.harness.enqueue_harness_apply_job", lambda *a, **k: None
    )
    monkeypatch.setattr(executor_module.caps, "snapshot", lambda: CAPS)


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


class TestTemplateCrud:
    def test_put_get_and_delete_round_trip(self, api_client, seeded):
        _, _, video = seeded
        body = api_client.put(
            f"/videos/{video.id}/editing/recipe_templates",
            json={"recipe_id": "subject_behind_text", "params": {"templateId": "glass"}},
        ).json()
        assert body["templates"]["subject_behind_text"] == {"templateId": "glass"}

        listed = api_client.get(f"/videos/{video.id}/editing/recipe_templates").json()
        assert listed["templates"]["subject_behind_text"] == {"templateId": "glass"}

        cleared = api_client.put(
            f"/videos/{video.id}/editing/recipe_templates",
            json={"recipe_id": "subject_behind_text", "params": {}},
        ).json()
        assert cleared["templates"] == {}

    def test_content_keys_are_refused_with_the_allowed_list(self, api_client, seeded):
        _, _, video = seeded
        response = api_client.put(
            f"/videos/{video.id}/editing/recipe_templates",
            json={
                "recipe_id": "subject_behind_text",
                "params": {"text": "HOUSE TITLE", "templateId": "glass"},
            },
        )
        assert response.status_code == 422
        assert "text" in response.json()["detail"]
        assert "templateId" in response.json()["detail"]  # the allowed list names it

    def test_non_scalar_values_are_refused(self, api_client, seeded):
        _, _, video = seeded
        response = api_client.put(
            f"/videos/{video.id}/editing/recipe_templates",
            json={
                "recipe_id": "tracked_callout",
                "params": {"accent": {"r": 255}},
            },
        )
        assert response.status_code == 422

    def test_unknown_recipes_are_a_404(self, api_client, seeded):
        _, _, video = seeded
        response = api_client.put(
            f"/videos/{video.id}/editing/recipe_templates",
            json={"recipe_id": "no_such_recipe", "params": {}},
        )
        assert response.status_code == 404


class TestPrecedence:
    def _template(self, db, project, params):
        row = HarnessRecipeTemplate(
            workspace_id=project.workspace_id,
            recipe_id="subject_behind_text",
            params=params,
        )
        db.add(row)
        db.commit()

    def test_team_template_fills_unset_keys(self, db_session, api_client, seeded):
        _, project, video = seeded
        self._template(db_session, project, {"templateId": "glass", "maskQuality": "better"})
        run = api_client.post(
            f"/videos/{video.id}/editing/runs",
            json={
                "recipe_id": "subject_behind_text",
                "params": {"range": {"start": 1.0, "end": 5.0}, "text": "HI"},
            },
        ).json()
        assert run["state"] == "planned", run.get("error_detail")
        assert run["params"]["templateId"] == "glass"
        assert run["params"]["maskQuality"] == "better"

    def test_the_users_own_learning_beats_the_team(self, db_session, api_client, seeded):
        user, project, video = seeded
        self._template(db_session, project, {"templateId": "glass"})
        for _ in range(3):
            db_session.add(
                HarnessRun(
                    project_id=project.id, video_id=video.id, created_by=user.id,
                    state="ready", recipe_id="subject_behind_text",
                    params={"templateId": "minimal"},
                )
            )
        db_session.commit()
        run = api_client.post(
            f"/videos/{video.id}/editing/runs",
            json={
                "recipe_id": "subject_behind_text",
                "params": {"range": {"start": 1.0, "end": 5.0}, "text": "HI"},
            },
        ).json()
        assert run["state"] == "planned", run.get("error_detail")
        assert run["params"]["templateId"] == "minimal"

    def test_explicit_params_beat_everything(self, db_session, api_client, seeded):
        _, project, video = seeded
        self._template(db_session, project, {"templateId": "glass"})
        run = api_client.post(
            f"/videos/{video.id}/editing/runs",
            json={
                "recipe_id": "subject_behind_text",
                "params": {
                    "range": {"start": 1.0, "end": 5.0},
                    "text": "HI",
                    "templateId": "mono",
                },
            },
        ).json()
        assert run["state"] == "planned", run.get("error_detail")
        assert run["params"]["templateId"] == "mono"

    def test_a_stale_whitelisted_key_never_resurfaces(self, db_session, seeded):
        """A key later removed from the whitelist is filtered on read."""
        _, project, _ = seeded
        row = HarnessRecipeTemplate(
            workspace_id=project.workspace_id,
            recipe_id="subject_behind_text",
            params={"templateId": "glass", "retiredKey": "x"},
        )
        db_session.add(row)
        db_session.commit()
        defaults = preferences.team_defaults(
            db_session, project.workspace_id, "subject_behind_text"
        )
        assert defaults == {"templateId": "glass"}
