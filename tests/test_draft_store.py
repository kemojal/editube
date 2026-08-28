"""The draft store: revisions, conflicts, adoption, and the rewired routes."""

from __future__ import annotations

from typing import Any

import pytest

from app.db.models import AiResult, RoughCutDraft, RoughCutDraftRevision, WorkspaceMember
from app.services import draft_store
from app.services.draft_store import DraftConflict


@pytest.fixture
def project_video(db_session, make_project, make_video, make_user):
    user = make_user()
    project = make_project(creator=user)
    video = make_video(project=project, duration=20)
    db_session.commit()
    return user, project, video


class TestStore:
    def test_first_save_creates_revision_one_with_checksum(self, db_session, project_video):
        _, project, video = project_video
        view = draft_store.save_draft(
            db_session, project.id, {"keepRanges": [{"start": 0, "end": 5}]},
            writer="editor", video_id=video.id,
        )
        assert view.revision == 1
        assert view.checksum and view.checksum.startswith("sha256:")
        assert view.last_writer == "editor"
        snap = (
            db_session.query(RoughCutDraftRevision)
            .filter(RoughCutDraftRevision.revision == 1)
            .one()
        )
        assert snap.parent_revision == 0

    def test_stale_expected_revision_conflicts_and_changes_nothing(
        self, db_session, project_video
    ):
        _, project, video = project_video
        draft_store.save_draft(db_session, project.id, {"a": 1}, writer="editor", video_id=video.id)
        draft_store.save_draft(
            db_session, project.id, {"a": 2}, writer="editor", expected_revision=1
        )
        with pytest.raises(DraftConflict) as exc:
            draft_store.save_draft(
                db_session, project.id, {"a": 99}, writer="editor", expected_revision=1
            )
        assert exc.value.current_revision == 2
        db_session.rollback()
        assert draft_store.get_draft(db_session, project.id).payload["a"] == 2

    def test_save_adopts_legacy_ai_result_payload_as_the_base(
        self, db_session, project_video
    ):
        _, project, video = project_video
        db_session.add(
            AiResult(
                video_id=video.id,
                result_type="rough_cut_draft",
                result_data={"keepRanges": [{"start": 1, "end": 2}], "legacyThing": True},
            )
        )
        db_session.commit()
        # A read sees the legacy payload at revision 0 without writing anything.
        view = draft_store.get_draft(db_session, project.id)
        assert view.row is None and view.revision == 0
        assert view.payload["legacyThing"] is True
        assert db_session.query(RoughCutDraft).count() == 0

    def test_save_mirrors_into_legacy_row_for_unmigrated_readers(
        self, db_session, project_video
    ):
        _, project, video = project_video
        draft_store.save_draft(
            db_session, project.id, {"keepRanges": []}, writer="editor", video_id=video.id
        )
        legacy = (
            db_session.query(AiResult)
            .filter(AiResult.video_id == video.id, AiResult.result_type == "rough_cut_draft")
            .one()
        )
        assert legacy.result_data == {"keepRanges": []}

    def test_mutate_retries_through_a_conflict(self, db_session, project_video, monkeypatch):
        _, project, video = project_video
        draft_store.save_draft(db_session, project.id, {"n": 0}, writer="editor", video_id=video.id)

        calls = {"n": 0}
        real_save = draft_store.save_draft

        def _racing_save(db, project_id, payload, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise DraftConflict(2, None)
            return real_save(db, project_id, payload, **kwargs)

        monkeypatch.setattr(draft_store, "save_draft", _racing_save)
        view = draft_store.mutate_draft(
            db_session, project.id, lambda p: {**p, "n": p.get("n", 0) + 1}, writer="auto_edit"
        )
        assert view.payload["n"] == 1
        assert calls["n"] == 2

    def test_mutate_without_create_is_a_noop_on_missing_draft(self, db_session, project_video):
        _, project, _ = project_video
        result = draft_store.mutate_draft(
            db_session, project.id, lambda p: {**p, "x": 1}, writer="effect_job", create=False
        )
        assert result is None
        assert db_session.query(RoughCutDraft).count() == 0

    def test_human_writer_stamps_user_edited_at_and_background_does_not(
        self, db_session, project_video
    ):
        _, project, video = project_video
        view = draft_store.save_draft(
            db_session, project.id, {"a": 1}, writer="auto_edit", video_id=video.id
        )
        assert view.user_edited_at is None
        view = draft_store.save_draft(db_session, project.id, {"a": 2}, writer="editor")
        assert view.user_edited_at is not None

    def test_load_revision_round_trips_the_snapshot(self, db_session, project_video):
        _, project, video = project_video
        first = draft_store.save_draft(
            db_session, project.id, {"a": 1}, writer="editor", video_id=video.id
        )
        draft_store.save_draft(db_session, project.id, {"a": 2}, writer="editor")
        payload = draft_store.load_revision(db_session, first.row.id, 1)
        assert payload == {"a": 1}


class TestDraftRoutes:
    def _get(self, api_client, video_id: int):
        return api_client.get(f"/videos/{video_id}/ai/rough-cut-draft")

    def test_get_never_writes(self, db_session, api_client, project_video):
        user, project, video = project_video
        api_client.login(user)
        response = self._get(api_client, video.id)
        assert response.status_code == 200
        assert response.json()["revision"] == 0
        assert db_session.query(RoughCutDraft).count() == 0
        assert db_session.query(AiResult).count() == 0

    def test_put_then_get_carries_revision_and_checksum(
        self, db_session, api_client, project_video
    ):
        user, project, video = project_video
        api_client.login(user)
        put = api_client.put(
            f"/videos/{video.id}/ai/rough-cut-draft",
            json={"keepRanges": [{"start": 0, "end": 3}], "rangeEditVersion": 3},
        )
        assert put.status_code == 200
        body = put.json()
        assert body["revision"] == 1 and body["checksum"].startswith("sha256:")
        got = self._get(api_client, video.id).json()
        assert got["revision"] == 1
        assert got["result_data"]["keepRanges"] == [{"start": 0, "end": 3}]

    def test_stale_revision_is_409_not_a_silent_overwrite(
        self, api_client, project_video
    ):
        user, _, video = project_video
        api_client.login(user)
        url = f"/videos/{video.id}/ai/rough-cut-draft"
        api_client.put(url, json={"keepRanges": [{"start": 0, "end": 3}]})
        api_client.put(url, json={"keepRanges": [{"start": 0, "end": 4}], "expected_revision": 1})
        stale = api_client.put(
            url, json={"keepRanges": [{"start": 0, "end": 9}], "expected_revision": 1}
        )
        assert stale.status_code == 409
        detail = stale.json()["detail"]
        assert detail["code"] == "draft_conflict" and detail["current_revision"] == 2
        current = self._get(api_client, video.id).json()
        assert current["result_data"]["keepRanges"] == [{"start": 0, "end": 4}]

    def test_partial_body_no_longer_wipes_structural_keys(
        self, api_client, project_video
    ):
        """The create-project wizard's 7-key seed used to reset the timeline."""
        user, _, video = project_video
        api_client.login(user)
        url = f"/videos/{video.id}/ai/rough-cut-draft"
        api_client.put(
            url,
            json={
                "keepRanges": [{"start": 0, "end": 3}],
                "clipAttributes": {"video:r1": {"adjust": {"exposure": 1}}},
                "timelineMediaItems": [{"id": "m1"}],
            },
        )
        # A partial save, like the wizard's: only keepRanges + flags.
        api_client.put(url, json={"keepRanges": [{"start": 0, "end": 2}], "showFillers": True})
        got = self._get(api_client, video.id).json()["result_data"]
        assert got["keepRanges"] == [{"start": 0, "end": 2}]
        assert got["clipAttributes"] == {"video:r1": {"adjust": {"exposure": 1}}}
        assert got["timelineMediaItems"] == [{"id": "m1"}]
        # And the phantom default injection is gone.
        assert "effectJobs" not in got

    def test_guest_workspace_member_cannot_put_the_timeline(
        self, db_session, api_client, make_user, project_video
    ):
        _, project, video = project_video
        guest = make_user()
        db_session.add(
            WorkspaceMember(workspace_id=project.workspace_id, user_id=guest.id, role="guest")
        )
        db_session.commit()
        api_client.login(guest)
        response = api_client.put(
            f"/videos/{video.id}/ai/rough-cut-draft", json={"keepRanges": []}
        )
        assert response.status_code == 403

    def test_unknown_extra_keys_still_round_trip(self, api_client, project_video):
        """`extra=\"allow\"` passthrough is load-bearing for undeclared keys."""
        user, _, video = project_video
        api_client.login(user)
        url = f"/videos/{video.id}/ai/rough-cut-draft"
        api_client.put(url, json={"textOverlays": [{"id": "t1", "text": "hi"}], "sourceDuration": 20})
        got = self._get(api_client, video.id).json()["result_data"]
        assert got["textOverlays"] == [{"id": "t1", "text": "hi"}]
        assert got["sourceDuration"] == 20
