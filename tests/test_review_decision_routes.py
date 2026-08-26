"""The endpoints that make a video's review state changeable.

These are the first route-level tests in the codebase (see `conftest.py`), and
they exist because the two status handlers previously carried duplicate
validation, recorded no provenance, and allowed any move at all.
"""

from __future__ import annotations

import pytest

from app.db.models import Comment, Notification, VideoApproval


@pytest.fixture(autouse=True)
def stub_delivery(monkeypatch):
    """Notifications are asserted through the database, not the wire."""
    import app.services.notifications as notifications_module

    monkeypatch.setattr(
        "app.jobs.queue.enqueue_push_notification_job", lambda *a, **kw: True
    )

    async def _noop(user_id, event):  # noqa: ANN001
        return None

    monkeypatch.setattr(
        notifications_module.notifications_ws_manager, "send_to_user", _noop
    )


@pytest.fixture
def review_ctx(db_session, make_user, make_project, make_video):
    owner = make_user(name="Ollie Owner")
    editor = make_user(name="Ed Editor")
    project = make_project(creator=owner, name="Launch")
    video = make_video(project, name="Hero Cut", uploader_id=editor.id)
    db_session.commit()
    return {"owner": owner, "editor": editor, "project": project, "video": video}


class StatusEndpointTests:
    def test_sets_a_legal_status_and_records_provenance(
        self, api_client, db_session, review_ctx
    ) -> None:
        video, owner = review_ctx["video"], review_ctx["owner"]
        api_client.login(owner)

        response = api_client.put(
            f"/videos/{video.id}/status", json={"status": "in_review"}
        )

        assert response.status_code == 200
        assert response.json()["status"] == "in_review"
        db_session.refresh(video)
        assert video.status_changed_by == owner.id
        assert video.status_changed_at is not None

    def test_refuses_an_illegal_move(self, api_client, review_ctx) -> None:
        # A cut cannot be approved without ever having been sent for review.
        api_client.login(review_ctx["owner"])

        response = api_client.put(
            f"/videos/{review_ctx['video'].id}/status", json={"status": "approved"}
        )

        assert response.status_code == 400
        assert "in_review" in response.json()["detail"]

    def test_refuses_an_unknown_status(self, api_client, review_ctx) -> None:
        api_client.login(review_ctx["owner"])
        response = api_client.put(
            f"/videos/{review_ctx['video'].id}/status", json={"status": "published"}
        )
        assert response.status_code == 400

    def test_both_status_routes_agree(
        self, api_client, db_session, review_ctx, make_video
    ) -> None:
        # The project-scoped route and the bare one are near-duplicates that
        # used to hold separate copies of the valid-status tuple.
        owner, project = review_ctx["owner"], review_ctx["project"]
        other = make_video(project, name="Second cut", uploader_id=owner.id)
        db_session.commit()
        api_client.login(owner)

        bare = api_client.put(f"/videos/{review_ctx['video'].id}/status", json={"status": "approved"})
        scoped = api_client.put(
            f"/projects/{project.id}/videos/{other.id}/status", json={"status": "approved"}
        )

        assert bare.status_code == scoped.status_code == 400

    def test_requires_authentication(self, api_client, review_ctx) -> None:
        response = api_client.put(
            f"/videos/{review_ctx['video'].id}/status", json={"status": "in_review"}
        )
        assert response.status_code == 401


class SendForReviewTests:
    def test_moves_the_cut_and_records_the_deadline(
        self, api_client, db_session, review_ctx
    ) -> None:
        video, project = review_ctx["video"], review_ctx["project"]
        api_client.login(review_ctx["owner"])

        response = api_client.post(
            f"/projects/{project.id}/videos/{video.id}/send-for-review",
            json={"due_at": "2026-09-01T17:00:00Z"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "in_review"
        assert body["review_due_at"] is not None

    def test_notifies_the_named_reviewers(
        self, api_client, db_session, review_ctx, make_user
    ) -> None:
        reviewer = make_user(name="Rita Reviewer")
        db_session.commit()
        api_client.login(review_ctx["owner"])

        api_client.post(
            f"/projects/{review_ctx['project'].id}/videos/{review_ctx['video'].id}/send-for-review",
            json={"reviewer_user_ids": [reviewer.id]},
        )

        rows = db_session.query(Notification).all()
        assert [row.user_id for row in rows] == [reviewer.id]
        assert rows[0].type == "review_requested"

    def test_falls_back_to_the_people_who_own_the_work(
        self, api_client, db_session, review_ctx
    ) -> None:
        # A solo creator should not have to nominate themselves.
        api_client.login(review_ctx["owner"])

        api_client.post(
            f"/projects/{review_ctx['project'].id}/videos/{review_ctx['video'].id}/send-for-review",
            json={},
        )

        notified = {row.user_id for row in db_session.query(Notification).all()}
        # Owner is the actor, so only the editor who uploaded it is told.
        assert notified == {review_ctx["editor"].id}


class ReviewDecisionTests:
    def _send(self, api_client, ctx):
        api_client.put(f"/videos/{ctx['video'].id}/status", json={"status": "in_review"})

    def test_approving_records_a_decision_and_moves_the_status(
        self, api_client, db_session, review_ctx
    ) -> None:
        api_client.login(review_ctx["owner"])
        self._send(api_client, review_ctx)

        response = api_client.post(
            f"/videos/{review_ctx['video'].id}/review-decision",
            json={"decision": "approved", "note": "Ship it."},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "approved"
        approval = db_session.query(VideoApproval).one()
        assert approval.decision == "approved"
        assert approval.actor_user_id == review_ctx["owner"].id
        assert approval.note == "Ship it."

    def test_the_response_carries_the_decision_summary(
        self, api_client, review_ctx
    ) -> None:
        # So the client can setQueryData without a follow-up fetch.
        api_client.login(review_ctx["owner"])
        self._send(api_client, review_ctx)

        body = api_client.post(
            f"/videos/{review_ctx['video'].id}/review-decision",
            json={"decision": "approved"},
        ).json()

        assert body["decision"]["decision"] == "approved"
        assert body["decision"]["actor_name"] == "Ollie Owner"
        assert body["decision"]["superseded"] is False

    def test_requesting_changes_moves_the_cut_back(
        self, api_client, review_ctx
    ) -> None:
        api_client.login(review_ctx["owner"])
        self._send(api_client, review_ctx)

        response = api_client.post(
            f"/videos/{review_ctx['video'].id}/review-decision",
            json={"decision": "changes_requested", "note": "Trim the intro."},
        )

        assert response.json()["status"] == "needs_changes"

    def test_open_change_requests_block_approval(
        self, api_client, db_session, review_ctx, make_comment
    ) -> None:
        make_comment(
            review_ctx["video"], kind="change_request", status="open", text="Fix the logo"
        )
        db_session.commit()
        api_client.login(review_ctx["owner"])
        self._send(api_client, review_ctx)

        response = api_client.post(
            f"/videos/{review_ctx['video'].id}/review-decision",
            json={"decision": "approved"},
        )

        # 409: the request is well-formed, the world just isn't ready for it.
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["blockers"][0]["code"] == "unresolved_change_requests"
        assert detail["blockers"][0]["count"] == 1

    def test_approve_with_notes_overrides_the_blockers(
        self, api_client, db_session, review_ctx, make_comment
    ) -> None:
        # Real clients sign off with two nits outstanding constantly.
        make_comment(review_ctx["video"], kind="change_request", status="open")
        db_session.commit()
        api_client.login(review_ctx["owner"])
        self._send(api_client, review_ctx)

        response = api_client.post(
            f"/videos/{review_ctx['video'].id}/review-decision",
            json={"decision": "approved", "override_blockers": True},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "approved"

    def test_resolved_change_requests_do_not_block(
        self, api_client, db_session, review_ctx, make_comment
    ) -> None:
        make_comment(review_ctx["video"], kind="change_request", status="resolved")
        db_session.commit()
        api_client.login(review_ctx["owner"])
        self._send(api_client, review_ctx)

        response = api_client.post(
            f"/videos/{review_ctx['video'].id}/review-decision",
            json={"decision": "approved"},
        )

        assert response.status_code == 200

    def test_a_plain_comment_does_not_block(
        self, api_client, db_session, review_ctx, make_comment
    ) -> None:
        make_comment(review_ctx["video"], kind="comment", status="open")
        db_session.commit()
        api_client.login(review_ctx["owner"])
        self._send(api_client, review_ctx)

        assert (
            api_client.post(
                f"/videos/{review_ctx['video'].id}/review-decision",
                json={"decision": "approved"},
            ).status_code
            == 200
        )

    def test_internal_change_requests_block_the_team(
        self, api_client, db_session, review_ctx, make_comment
    ) -> None:
        # Unlike the client-facing gate: the team can see and act on these.
        make_comment(
            review_ctx["video"], kind="change_request", status="open", visibility="team"
        )
        db_session.commit()
        api_client.login(review_ctx["owner"])
        self._send(api_client, review_ctx)

        response = api_client.post(
            f"/videos/{review_ctx['video'].id}/review-decision",
            json={"decision": "approved"},
        )

        assert response.status_code == 409

    def test_notifies_the_other_side(
        self, api_client, db_session, review_ctx
    ) -> None:
        api_client.login(review_ctx["owner"])
        self._send(api_client, review_ctx)
        db_session.query(Notification).delete()
        db_session.commit()

        api_client.post(
            f"/videos/{review_ctx['video'].id}/review-decision",
            json={"decision": "changes_requested"},
        )

        rows = db_session.query(Notification).all()
        assert [row.user_id for row in rows] == [review_ctx["editor"].id]
        assert rows[0].type == "changes_requested"
        assert "requested changes" in rows[0].message

    def test_rejects_an_unknown_decision(self, api_client, review_ctx) -> None:
        api_client.login(review_ctx["owner"])
        response = api_client.post(
            f"/videos/{review_ctx['video'].id}/review-decision",
            json={"decision": "maybe"},
        )
        assert response.status_code == 422

    def test_missing_video_is_a_404(self, api_client, review_ctx) -> None:
        api_client.login(review_ctx["owner"])
        assert (
            api_client.post(
                "/videos/999999/review-decision", json={"decision": "approved"}
            ).status_code
            == 404
        )
