"""The review inbox — "what needs me right now", across every project.

The dashboard answered "what exists"; nothing answered "what is my next
action". These tests pin the section rules, because the whole value of the
page is that a row appears for a reason the user would agree with.
"""

from __future__ import annotations

import pytest

from app.db.models import ProjectCollaborator, ReviewLink, ReviewSession
from app.services.video_status import (
    DECISION_APPROVED,
    STATUS_IN_REVIEW,
    apply_video_status,
    record_decision,
    supersede_open_decisions,
)


@pytest.fixture
def ctx(db_session, make_user, make_project, make_video):
    owner = make_user(name="Ollie Owner")
    editor = make_user(name="Ed Editor")
    outsider = make_user(name="Otto Outsider")
    project = make_project(creator=owner, name="Launch")
    # The editor reaches the project as a collaborator. Without this they have
    # no access route at all — which is exactly what `outsider` is here to
    # prove, and why the inbox must show them nothing.
    db_session.add(
        ProjectCollaborator(project_id=project.id, user_id=editor.id, role="editor")
    )
    db_session.commit()
    return {
        "owner": owner,
        "editor": editor,
        "outsider": outsider,
        "project": project,
    }


def _inbox(api_client, user):
    api_client.login(user)
    response = api_client.get("/reviews/inbox")
    assert response.status_code == 200
    return response.json()


class NeedsYouTests:
    def test_a_cut_in_review_lands_in_front_of_you(
        self, api_client, db_session, ctx, make_video
    ) -> None:
        video = make_video(ctx["project"], name="Hero Cut", uploader_id=ctx["editor"].id)
        apply_video_status(db_session, video, STATUS_IN_REVIEW, actor_user_id=ctx["editor"].id)
        db_session.commit()

        inbox = _inbox(api_client, ctx["owner"])

        assert [row["name"] for row in inbox["needs_you"]] == ["Hero Cut"]
        assert inbox["needs_you"][0]["reason"] == "review_requested"

    def test_a_cut_you_sent_is_waiting_on_someone_else(
        self, api_client, db_session, ctx, make_video
    ) -> None:
        # The cut is in review either way; whose problem it is depends on who
        # pushed it there.
        video = make_video(ctx["project"], name="Hero Cut", uploader_id=ctx["editor"].id)
        apply_video_status(db_session, video, STATUS_IN_REVIEW, actor_user_id=ctx["editor"].id)
        db_session.commit()

        inbox = _inbox(api_client, ctx["editor"])

        assert inbox["needs_you"] == []
        assert [row["name"] for row in inbox["waiting_on_others"]] == ["Hero Cut"]

    def test_changes_requested_goes_back_to_the_uploader(
        self, api_client, db_session, ctx, make_video
    ) -> None:
        video = make_video(ctx["project"], name="Hero Cut", uploader_id=ctx["editor"].id)
        apply_video_status(db_session, video, STATUS_IN_REVIEW, actor_user_id=ctx["editor"].id)
        record_decision(
            db_session, video, "changes_requested", actor_user_id=ctx["owner"].id
        )
        db_session.commit()

        editor_inbox = _inbox(api_client, ctx["editor"])
        owner_inbox = _inbox(api_client, ctx["owner"])

        assert [r["reason"] for r in editor_inbox["needs_you"]] == ["changes_requested"]
        # The reviewer already gave their answer; it is not their move now.
        assert owner_inbox["needs_you"] == []

    def test_comments_assigned_to_you_surface_the_video(
        self, api_client, db_session, ctx, make_video, make_comment
    ) -> None:
        video = make_video(ctx["project"], name="Hero Cut", uploader_id=ctx["editor"].id)
        make_comment(video, status="open", assignee_user_id=ctx["editor"].id)
        make_comment(video, status="open", assignee_user_id=ctx["editor"].id)
        db_session.commit()

        inbox = _inbox(api_client, ctx["editor"])

        assert len(inbox["needs_you"]) == 1
        assert inbox["needs_you"][0]["reason"] == "assigned_comments"
        assert inbox["needs_you"][0]["assigned_to_you"] == 2

    def test_resolved_assignments_do_not_surface(
        self, api_client, db_session, ctx, make_video, make_comment
    ) -> None:
        video = make_video(ctx["project"], uploader_id=ctx["editor"].id)
        make_comment(video, status="resolved", assignee_user_id=ctx["editor"].id)
        db_session.commit()

        assert _inbox(api_client, ctx["editor"])["needs_you"] == []

    def test_a_video_appears_once_even_with_several_claims(
        self, api_client, db_session, ctx, make_video, make_comment
    ) -> None:
        video = make_video(ctx["project"], name="Hero Cut", uploader_id=ctx["editor"].id)
        apply_video_status(db_session, video, STATUS_IN_REVIEW, actor_user_id=ctx["editor"].id)
        make_comment(video, status="open", assignee_user_id=ctx["owner"].id)
        db_session.commit()

        inbox = _inbox(api_client, ctx["owner"])

        assert len(inbox["needs_you"]) == 1
        # Surfaced for the stronger reason, but the assignment count survives.
        assert inbox["needs_you"][0]["reason"] == "review_requested"
        assert inbox["needs_you"][0]["assigned_to_you"] == 1

    def test_an_idle_cut_is_nobodys_problem(
        self, api_client, db_session, ctx, make_video
    ) -> None:
        make_video(ctx["project"], uploader_id=ctx["editor"].id)  # in_progress
        db_session.commit()

        inbox = _inbox(api_client, ctx["owner"])

        assert inbox["needs_you"] == []
        assert inbox["waiting_on_others"] == []

    def test_counts_travel_with_the_row(
        self, api_client, db_session, ctx, make_video, make_comment
    ) -> None:
        video = make_video(ctx["project"], uploader_id=ctx["editor"].id)
        apply_video_status(db_session, video, STATUS_IN_REVIEW, actor_user_id=ctx["editor"].id)
        make_comment(video, kind="change_request", status="open")
        make_comment(video, kind="comment", status="open")
        db_session.commit()

        row = _inbox(api_client, ctx["owner"])["needs_you"][0]

        assert row["open_comments"] == 2
        assert row["open_change_requests"] == 1


class AccessTests:
    def test_you_only_see_projects_you_can_reach(
        self, api_client, db_session, ctx, make_video
    ) -> None:
        video = make_video(ctx["project"], uploader_id=ctx["editor"].id)
        apply_video_status(db_session, video, STATUS_IN_REVIEW, actor_user_id=ctx["editor"].id)
        db_session.commit()

        assert _inbox(api_client, ctx["outsider"])["needs_you"] == []

    def test_requires_authentication(self, api_client) -> None:
        assert api_client.get("/reviews/inbox").status_code == 401


class WaitingOnOthersTests:
    def test_reports_whether_the_client_ever_opened_it(
        self, api_client, db_session, ctx, make_video
    ) -> None:
        # The difference between "they're thinking about it" and "they never
        # saw it" is the difference between waiting and chasing.
        video = make_video(ctx["project"], uploader_id=ctx["editor"].id)
        apply_video_status(db_session, video, STATUS_IN_REVIEW, actor_user_id=ctx["editor"].id)
        db_session.commit()

        row = _inbox(api_client, ctx["editor"])["waiting_on_others"][0]
        assert row["opened"] is False

        link = ReviewLink(video_id=video.id, token="tok-inbox")
        db_session.add(link)
        db_session.flush()
        db_session.add(
            ReviewSession(review_link_id=link.id, fingerprint="fp", guest_name="Client")
        )
        db_session.commit()

        row = _inbox(api_client, ctx["editor"])["waiting_on_others"][0]
        assert row["opened"] is True
        assert row["last_opened_at"] is not None


class RecentlyClosedTests:
    def test_lists_live_approvals(
        self, api_client, db_session, ctx, make_video
    ) -> None:
        video = make_video(ctx["project"], name="Hero Cut", uploader_id=ctx["editor"].id)
        apply_video_status(db_session, video, STATUS_IN_REVIEW, actor_user_id=ctx["editor"].id)
        record_decision(db_session, video, DECISION_APPROVED, actor_user_id=ctx["owner"].id)
        db_session.commit()

        closed = _inbox(api_client, ctx["owner"])["recently_closed"]

        assert [row["name"] for row in closed] == ["Hero Cut"]
        assert closed[0]["approved_by"] == "Ollie Owner"

    def test_superseded_approvals_drop_off(
        self, api_client, db_session, ctx, make_video
    ) -> None:
        # "v2 was approved" stops being current news once v3 lands.
        v1 = make_video(ctx["project"], version=1, uploader_id=ctx["editor"].id)
        v2 = make_video(ctx["project"], version=2, uploader_id=ctx["editor"].id)
        apply_video_status(db_session, v1, STATUS_IN_REVIEW, actor_user_id=ctx["editor"].id)
        record_decision(db_session, v1, DECISION_APPROVED, actor_user_id=ctx["owner"].id)
        supersede_open_decisions(db_session, [v1.id], superseded_by_video_id=v2.id)
        db_session.commit()

        assert _inbox(api_client, ctx["owner"])["recently_closed"] == []


class SummaryTests:
    def test_counts_everything_still_moving(
        self, api_client, db_session, ctx, make_video
    ) -> None:
        a = make_video(ctx["project"], uploader_id=ctx["editor"].id)
        b = make_video(ctx["project"], uploader_id=ctx["editor"].id)
        make_video(ctx["project"], uploader_id=ctx["editor"].id)  # stays in_progress
        for video in (a, b):
            apply_video_status(
                db_session, video, STATUS_IN_REVIEW, actor_user_id=ctx["editor"].id
            )
        db_session.commit()

        api_client.login(ctx["owner"])
        body = api_client.get("/reviews/inbox/summary").json()

        assert body["needs_you_count"] == 2

    def test_zero_for_someone_with_no_projects(self, api_client, ctx) -> None:
        api_client.login(ctx["outsider"])
        assert api_client.get("/reviews/inbox/summary").json()["needs_you_count"] == 0

    def test_the_badge_matches_the_page_a_cut_you_sent_does_not_count(
        self, api_client, db_session, ctx, make_video
    ) -> None:
        # The badge saying 3 while the page shows 1 teaches people it lies.
        video = make_video(ctx["project"], uploader_id=ctx["editor"].id)
        apply_video_status(db_session, video, STATUS_IN_REVIEW, actor_user_id=ctx["editor"].id)
        db_session.commit()

        api_client.login(ctx["editor"])
        assert api_client.get("/reviews/inbox/summary").json()["needs_you_count"] == 0
        api_client.login(ctx["owner"])
        assert api_client.get("/reviews/inbox/summary").json()["needs_you_count"] == 1

    def test_the_badge_counts_comments_assigned_to_you(
        self, api_client, db_session, ctx, make_video, make_comment
    ) -> None:
        video = make_video(ctx["project"], uploader_id=ctx["owner"].id)
        make_comment(video, status="open", assignee_user_id=ctx["editor"].id)
        db_session.commit()

        api_client.login(ctx["editor"])
        assert api_client.get("/reviews/inbox/summary").json()["needs_you_count"] == 1
