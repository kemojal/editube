"""Guest-side decisions: approve, send back, and see that a newer cut exists.

Three things are covered that the product could not previously do:

* a guest approval that actually moves `Video.status` (it used to write
  `ReviewSession.approved_at` and stop, so the editor saw nothing change);
* declining at all — there was no reject action anywhere in the product;
* `GET /review/{token}/versions`, which raised `AttributeError` on every call
  because it read `link.project_id`, a column `ReviewLink` does not have.
"""

from __future__ import annotations

import asyncio

import pytest

from app.db.models import Comment, Notification, ReviewLink, ReviewSession, VideoApproval


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def stub_delivery(monkeypatch):
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
def guest(db_session, make_user, make_project, make_video):
    owner = make_user(name="Ollie Owner")
    editor = make_user(name="Ed Editor")
    project = make_project(creator=owner, name="Launch")
    video = make_video(
        project, name="Hero Cut", uploader_id=editor.id, status="in_review"
    )
    link = ReviewLink(video_id=video.id, token="tok-guest", allow_comments=True)
    db_session.add(link)
    db_session.flush()
    session = ReviewSession(
        review_link_id=link.id,
        fingerprint="fp",
        guest_name="Sarah Client",
        guest_email="sarah@client.test",
    )
    db_session.add(session)
    db_session.commit()
    return {
        "owner": owner,
        "editor": editor,
        "project": project,
        "video": video,
        "link": link,
        "session": session,
    }


class GuestApprovalTests:
    def _approve(self, db_session, guest, approved=True):
        from app.api.models.review_links import PublicReviewApproveRequest
        from app.api.routes.review_links import approve

        return _run(
            approve(
                token=guest["link"].token,
                body=PublicReviewApproveRequest(
                    session_id=guest["session"].id, approved=approved
                ),
                db=db_session,
            )
        )

    def test_approval_moves_the_video_not_just_the_session(
        self, db_session, guest
    ) -> None:
        # The whole point: the editor's app should change, not just a row
        # nobody looks at.
        self._approve(db_session, guest)

        db_session.refresh(guest["video"])
        assert guest["video"].status == "approved"
        assert guest["session"].approved_at is not None

    def test_approval_is_recorded_against_the_version(self, db_session, guest) -> None:
        self._approve(db_session, guest)

        approval = db_session.query(VideoApproval).one()
        assert approval.video_id == guest["video"].id
        assert approval.decision == "approved"
        assert approval.review_session_id == guest["session"].id
        assert approval.actor_user_id is None

    def test_approval_notifies_owner_and_uploader(self, db_session, guest) -> None:
        self._approve(db_session, guest)

        rows = db_session.query(Notification).all()
        assert {row.user_id for row in rows} == {
            guest["owner"].id,
            guest["editor"].id,
        }
        assert "Sarah Client approved" in rows[0].message

    def test_withdrawing_approval_puts_the_cut_back_in_review(
        self, db_session, guest
    ) -> None:
        self._approve(db_session, guest)
        self._approve(db_session, guest, approved=False)

        db_session.refresh(guest["video"])
        assert guest["video"].status == "in_review"
        assert guest["session"].approved_at is None

    def test_open_change_requests_block_approval(
        self, db_session, guest, make_comment
    ) -> None:
        make_comment(guest["video"], kind="change_request", status="open")
        db_session.commit()

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as excinfo:
            self._approve(db_session, guest)

        assert excinfo.value.status_code == 400
        db_session.refresh(guest["video"])
        assert guest["video"].status == "in_review"

    def test_internal_change_requests_do_not_block_the_client(
        self, db_session, guest, make_comment
    ) -> None:
        # The client cannot see internal notes, so blocking on them would
        # strand them with no way to act.
        make_comment(
            guest["video"], kind="change_request", status="open", visibility="team"
        )
        db_session.commit()

        self._approve(db_session, guest)

        db_session.refresh(guest["video"])
        assert guest["video"].status == "approved"


class GuestRequestChangesTests:
    def _request(self, db_session, guest, **kwargs):
        from app.api.models.review_links import PublicReviewRequestChangesRequest
        from app.api.routes.review_links import request_changes

        return _run(
            request_changes(
                token=guest["link"].token,
                body=PublicReviewRequestChangesRequest(
                    session_id=guest["session"].id, **kwargs
                ),
                db=db_session,
            )
        )

    def test_sends_the_cut_back(self, db_session, guest) -> None:
        result = self._request(db_session, guest, note="The hook drags.")

        assert result["status"] == "needs_changes"
        db_session.refresh(guest["video"])
        assert guest["video"].status == "needs_changes"

    def test_the_note_becomes_a_change_request_on_the_timeline(
        self, db_session, guest
    ) -> None:
        # So it lands in the editor's revision checklist rather than living
        # only in a status field.
        result = self._request(db_session, guest, note="Trim the intro to 8s.")

        comment = db_session.query(Comment).one()
        assert comment.id == result["comment_id"]
        assert comment.kind == "change_request"
        assert comment.status == "open"
        assert comment.text == "Trim the intro to 8s."
        assert comment.guest_name == "Sarah Client"

    def test_no_comment_is_created_when_asked_not_to(self, db_session, guest) -> None:
        result = self._request(db_session, guest, note="Nope.", create_comment=False)

        assert result["comment_id"] is None
        assert db_session.query(Comment).count() == 0

    def test_an_empty_note_creates_no_comment(self, db_session, guest) -> None:
        result = self._request(db_session, guest, note="   ")

        assert result["comment_id"] is None
        assert db_session.query(Comment).count() == 0

    def test_it_records_a_decision(self, db_session, guest) -> None:
        self._request(db_session, guest, note="Not yet.")

        approval = db_session.query(VideoApproval).one()
        assert approval.decision == "changes_requested"
        assert approval.note == "Not yet."

    def test_it_withdraws_a_prior_approval(self, db_session, guest) -> None:
        from app.api.models.review_links import PublicReviewApproveRequest
        from app.api.routes.review_links import approve

        _run(
            approve(
                token=guest["link"].token,
                body=PublicReviewApproveRequest(session_id=guest["session"].id),
                db=db_session,
            )
        )

        self._request(db_session, guest, note="Actually, one more thing.")

        assert guest["session"].approved_at is None

    def test_it_notifies_the_team(self, db_session, guest) -> None:
        self._request(db_session, guest, note="Send it back.")

        rows = db_session.query(Notification).all()
        assert {row.user_id for row in rows} == {
            guest["owner"].id,
            guest["editor"].id,
        }
        assert rows[0].type == "changes_requested"


class ReviewVersionsEndpointTests:
    def _versions(self, db_session, token):
        from app.api.routes.review_links import list_review_versions

        return list_review_versions(token=token, db=db_session)

    def test_it_does_not_crash(self, db_session, guest) -> None:
        # Regression: this raised AttributeError on every call, because
        # ReviewLink has no project_id column and no project relationship.
        result = self._versions(db_session, guest["link"].token)

        assert result["ok"] is True

    def test_it_reports_the_current_version(self, db_session, guest) -> None:
        result = self._versions(db_session, guest["link"].token)

        assert result["current_version"] == guest["video"].version
        assert len(result["items"]) == 1
        assert result["items"][0]["is_current"] is True

    def test_it_finds_siblings_through_the_video_chain(
        self, db_session, guest, make_video
    ) -> None:
        v2 = make_video(
            guest["project"],
            name="Hero Cut v2",
            version=2,
            version_group_id=guest["video"].version_group_id,
            uploader_id=guest["editor"].id,
        )
        db_session.add(ReviewLink(video_id=v2.id, token="tok-v2"))
        db_session.commit()

        result = self._versions(db_session, guest["link"].token)

        assert [item["version"] for item in result["items"]] == [1, 2]
        assert result["latest_version"] == 2
        assert result["latest_token"] == "tok-v2"
        # This is what lets the guest page say "v2 is available".
        assert result["current_version"] == 1

    def test_revoked_links_are_excluded(
        self, db_session, guest, make_video
    ) -> None:
        from datetime import datetime, timezone

        v2 = make_video(
            guest["project"],
            version=2,
            version_group_id=guest["video"].version_group_id,
            uploader_id=guest["editor"].id,
        )
        db_session.add(
            ReviewLink(
                video_id=v2.id,
                token="tok-revoked",
                revoked_at=datetime.now(timezone.utc),
            )
        )
        db_session.commit()

        result = self._versions(db_session, guest["link"].token)

        assert [item["token"] for item in result["items"]] == ["tok-guest"]

    def test_it_carries_each_versions_status(
        self, db_session, guest, make_video
    ) -> None:
        result = self._versions(db_session, guest["link"].token)
        assert result["items"][0]["status"] == "in_review"
