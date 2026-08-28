"""End-to-end harness lifecycle over the HTTP surface.

REDIS_URL is stripped by the conftest guard, so `apply` runs inline through
the real `execute_apply` body — the same code a worker runs. The only fake is
the segmentation job itself (`_run_effect_job`), which is patched to complete
its effect row the way the real job does, minus ffmpeg.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.db.models import AiResult, HarnessRun, WorkspaceMember
from app.services import draft_store
from app.services.harness import executor as executor_module
from app.services.harness.schemas import entity_id

SEG_SNAPSHOT = {
    "version": 1,
    "capturedAt": "2026-08-29T00:00:00+00:00",
    "capabilities": {
        "segmentation": {
            "key": "segmentation",
            "available": True,
            "provider": "local",
            "limits": {"maxClipSeconds": 120},
            "detail": {"autoMatte": True, "pointPrompt": False, "propagate": False},
        },
        "queue": {"key": "queue", "available": False, "reason": "test"},
    },
}


@pytest.fixture(autouse=True)
def never_enqueue_for_real(monkeypatch):
    """Force the inline-apply path regardless of environment.

    The conftest deletes REDIS_URL, but importing `app.main` (inside the
    api_client fixture) re-runs `load_dotenv()`, which can repopulate it from a
    developer's `.env` — at which point apply would enqueue onto a *real* local
    queue and a live dev worker would pick it up. Patch the helper in the
    module that calls it, exactly as the conftest guard instructs.
    """
    monkeypatch.setattr(
        "app.api.routes.harness.enqueue_harness_apply_job", lambda *a, **k: None
    )


@pytest.fixture
def seg_available(monkeypatch):
    monkeypatch.setattr("app.services.harness.executor.caps.snapshot", lambda: SEG_SNAPSHOT)


@pytest.fixture
def fake_effect_job(db_session, monkeypatch):
    """Complete the staged effect row the way the real job would."""
    calls: list[int] = []

    def _fake(result_id: int) -> None:
        calls.append(result_id)
        row = db_session.query(AiResult).filter(AiResult.id == result_id).first()
        data = dict(row.result_data or {})
        data.update(
            {"status": "completed", "progress": 100,
             "outputUrl": f"https://cdn.example.test/cutout-{result_id}.webm"}
        )
        row.status = "completed"
        row.result_data = data
        db_session.commit()

    monkeypatch.setattr(executor_module, "_run_effect_job", _fake)
    return calls


@pytest.fixture
def editor(db_session, api_client, make_user, make_project, make_video):
    user = make_user()
    project = make_project(creator=user)
    video = make_video(project=project, duration=20)
    db_session.commit()
    api_client.login(user)
    return api_client, user, project, video


def _create_run(client, video_id: int, **params: Any):
    return client.post(
        f"/videos/{video_id}/editing/runs",
        json={
            "recipe_id": "subject_behind_text",
            "params": {
                "range": {"start": 2.0, "end": 8.0},
                "text": "BIG IDEA",
                **params,
            },
        },
    )


class TestLifecycle:
    def test_plan_approve_apply_verify_revert(
        self, db_session, editor, seg_available, fake_effect_job
    ):
        client, user, project, video = editor
        # A draft with prior human work, so revert-identity is meaningful.
        client.put(
            f"/videos/{video.id}/ai/rough-cut-draft",
            json={"keepRanges": [{"start": 0, "end": 20}], "rangeEditVersion": 3},
        )
        base = draft_store.get_draft(db_session, project.id)

        created = _create_run(client, video.id)
        assert created.status_code == 200
        run = created.json()
        assert run["state"] == "planned"
        assert run["base_draft_revision"] == base.revision
        assert len(run["operations"]) == 3
        assert run["estimates"]["operationCount"] == 3

        # Approval carries the reviewed checksum — a stale one is refused.
        stale = client.post(
            f"/editing/runs/{run['id']}/approve", json={"plan_checksum": "sha256:wrong"}
        )
        assert stale.status_code == 409

        approved = client.post(
            f"/editing/runs/{run['id']}/approve",
            json={"plan_checksum": run["plan_checksum"]},
        )
        assert approved.json()["state"] == "approved"

        applied = client.post(
            f"/editing/runs/{run['id']}/apply",
            json={"expected_revision": base.revision},
        )
        body = applied.json()
        assert body["state"] == "ready", body.get("error_detail")
        assert body["verification_report"]["status"] in {"pass", "warnings"}
        assert fake_effect_job, "staging must run the effect job"

        after = draft_store.get_draft(db_session, project.id)
        assert after.revision == base.revision + 1
        fg_id = entity_id(run["id"], "fg")
        items = {i["id"]: i for i in after.payload["timelineMediaItems"]}
        assert fg_id in items and items[fg_id]["audioEnabled"] is False
        clip = after.payload["clipAttributes"][f"media:{fg_id}"]
        assert clip["processing"]["remove_bg"]["status"] == "completed"
        assert after.payload["textOverlays"][0]["text"] == "BIG IDEA"

        # Refresh-proof revert: a brand-new request, addressed by run id.
        reverted = client.post(f"/editing/runs/{run['id']}/revert")
        assert reverted.json()["state"] == "reverted"
        # The inverse manifest survives revert — the audit trail the Director
        # used to destroy.
        assert reverted.json()["applied_manifest"] is not None
        final = draft_store.get_draft(db_session, project.id)
        assert final.payload["keepRanges"] == base.payload["keepRanges"]
        assert fg_id not in {
            i.get("id") for i in final.payload.get("timelineMediaItems", [])
        }
        assert "textOverlays" not in final.payload

    def test_apply_is_idempotent_against_double_click(
        self, db_session, editor, seg_available, fake_effect_job
    ):
        client, _, project, video = editor
        run = _create_run(client, video.id).json()
        client.post(
            f"/editing/runs/{run['id']}/approve", json={"plan_checksum": run["plan_checksum"]}
        )
        first = client.post(f"/editing/runs/{run['id']}/apply", json={})
        assert first.json()["state"] == "ready"
        second = client.post(f"/editing/runs/{run['id']}/apply", json={})
        assert second.status_code == 409
        view = draft_store.get_draft(db_session, project.id)
        assert len(view.payload["timelineMediaItems"]) == 1

    def test_draft_moved_during_staging_conflicts_instead_of_overwriting(
        self, db_session, editor, seg_available, monkeypatch
    ):
        client, _, project, video = editor
        run = _create_run(client, video.id).json()
        client.post(
            f"/editing/runs/{run['id']}/approve", json={"plan_checksum": run["plan_checksum"]}
        )

        def _racing_effect(result_id: int) -> None:
            # The user keeps editing while segmentation renders.
            draft_store.save_draft(
                db_session, project.id,
                {"keepRanges": [{"start": 0, "end": 9}], "rangeEditVersion": 3},
                writer="editor",
            )
            row = db_session.query(AiResult).filter(AiResult.id == result_id).first()
            data = dict(row.result_data or {})
            data.update({"status": "completed", "progress": 100,
                         "outputUrl": "https://cdn.example.test/x.webm"})
            row.status = "completed"
            row.result_data = data
            db_session.commit()

        monkeypatch.setattr(executor_module, "_run_effect_job", _racing_effect)
        applied = client.post(f"/editing/runs/{run['id']}/apply", json={})
        body = applied.json()
        assert body["state"] == "conflicted"
        assert body["error_code"] == "draft_moved"
        # The user's racing edit is exactly what survived.
        view = draft_store.get_draft(db_session, project.id)
        assert view.payload["keepRanges"] == [{"start": 0, "end": 9}]
        assert "timelineMediaItems" not in view.payload

    def test_failed_segmentation_never_mutates_the_draft(
        self, db_session, editor, seg_available, monkeypatch
    ):
        client, _, project, video = editor
        client.put(
            f"/videos/{video.id}/ai/rough-cut-draft",
            json={"keepRanges": [{"start": 0, "end": 20}], "rangeEditVersion": 3},
        )
        before = draft_store.get_draft(db_session, project.id)

        def _failing_effect(result_id: int) -> None:
            row = db_session.query(AiResult).filter(AiResult.id == result_id).first()
            data = dict(row.result_data or {})
            data.update({"status": "failed", "error": "No subject found."})
            row.status = "failed"
            row.result_data = data
            db_session.commit()

        monkeypatch.setattr(executor_module, "_run_effect_job", _failing_effect)
        run = _create_run(client, video.id).json()
        client.post(
            f"/editing/runs/{run['id']}/approve", json={"plan_checksum": run["plan_checksum"]}
        )
        applied = client.post(f"/editing/runs/{run['id']}/apply", json={})
        body = applied.json()
        assert body["state"] == "failed"
        assert "No subject found" in (body["error_detail"] or "")
        after = draft_store.get_draft(db_session, project.id)
        assert after.revision == before.revision
        assert after.payload == before.payload

    def test_toggling_an_operation_disables_dependents_and_voids_approval(
        self, editor, seg_available
    ):
        client, _, _, video = editor
        run = _create_run(client, video.id).json()
        client.post(
            f"/editing/runs/{run['id']}/approve", json={"plan_checksum": run["plan_checksum"]}
        )
        patched = client.patch(
            f"/editing/runs/{run['id']}/plan",
            json={"operation_key": "fg", "enabled": False},
        )
        body = patched.json()
        assert body["state"] == "planned"  # approval voided
        states = {op["operation_key"]: op["state"] for op in body["operations"]}
        assert states["fg"] == "disabled"
        assert states["mask"] == "disabled"  # cascade
        assert states["text"] == "pending"
        assert body["plan_checksum"] != run["plan_checksum"]


class TestGating:
    def test_capability_unavailable_fails_the_run_honestly(self, editor, monkeypatch):
        client, _, _, video = editor
        monkeypatch.setattr(
            "app.services.harness.executor.caps.snapshot",
            lambda: {"capabilities": {"segmentation": {"key": "segmentation",
                                                        "available": False,
                                                        "reason": "no provider installed"}}},
        )
        run = _create_run(client, video.id).json()
        assert run["state"] == "failed"
        assert run["error_code"] == "capability_unavailable"
        assert "no provider installed" in run["error_detail"]

    def test_guest_cannot_create_or_apply_runs(
        self, db_session, editor, make_user, seg_available
    ):
        client, _, project, video = editor
        run = _create_run(client, video.id).json()
        guest = make_user()
        db_session.add(
            WorkspaceMember(workspace_id=project.workspace_id, user_id=guest.id, role="guest")
        )
        db_session.commit()
        client.login(guest)
        assert _create_run(client, video.id).status_code == 403
        assert (
            client.post(
                f"/editing/runs/{run['id']}/approve",
                json={"plan_checksum": run["plan_checksum"]},
            ).status_code
            == 403
        )

    def test_stale_active_run_is_failed_by_reconcile_on_get(
        self, db_session, editor, seg_available, monkeypatch
    ):
        client, _, _, video = editor
        run_id = _create_run(client, video.id).json()["id"]
        row = db_session.query(HarnessRun).filter(HarnessRun.id == run_id).one()
        row.state = "staging"
        db_session.commit()
        monkeypatch.setattr(executor_module, "STALE_ACTIVE_SECONDS", -1)
        got = client.get(f"/editing/runs/{run_id}").json()
        assert got["state"] == "failed"
        assert got["error_code"] == "worker_died"

    def test_capabilities_endpoint_reports_recipes(self, editor, seg_available, monkeypatch):
        client, _, _, video = editor
        monkeypatch.setattr(
            "app.api.routes.harness.caps.snapshot", lambda: SEG_SNAPSHOT
        )
        got = client.get(f"/videos/{video.id}/editing/capabilities").json()
        recipes = {r["id"]: r for r in got["recipes"]}
        assert recipes["subject_behind_text"]["available"] is True
        assert got["capabilities"]["capabilities"]["segmentation"]["available"] is True
