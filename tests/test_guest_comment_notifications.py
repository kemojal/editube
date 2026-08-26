"""Regression cover for the worst bug in the review loop.

`POST /review/{token}/comments` used to nest its entire notification block
inside `if handles:`. A client could leave twenty comments and, unless they
happened to type an @mention, the editor was never told — not by push, not by
email, not in the app. Client feedback rotting unseen is precisely the failure
this product exists to remove.

These tests assert the owner and uploader are notified for a plain guest
comment, and that volume is handled by coalescing rather than by silence.
"""

from __future__ import annotations

import asyncio

import pytest

from app.db.models import Comment, Notification, ReviewLink, ReviewSession
from app.services.notifications import TYPE_CLIENT_COMMENT, TYPE_MENTION


@pytest.fixture
def guest_setup(db_session, make_user, make_project, make_video):
    """An editor-owned video shared with a client through a review link."""
    owner = make_user(name="Ollie Owner", email="owner@example.test")
    uploader = make_user(name="Ed Editor", email="editor@example.test")
    project = make_project(creator=owner, name="Summer campaign")
    video = make_video(project, name="Summer Reel", uploader_id=uploader.id)
    link = ReviewLink(video_id=video.id, token="guest-token", allow_comments=True)
    db_session.add(link)
    db_session.flush()
    session = ReviewSession(
        review_link_id=link.id,
        fingerprint="fp-1",
        guest_name="Sarah Client",
        guest_email="sarah@client.test",
    )
    db_session.add(session)
    db_session.commit()
    return {
        "owner": owner,
        "uploader": uploader,
        "project": project,
        "video": video,
        "link": link,
        "session": session,
    }


@pytest.fixture
def post_guest_comment(db_session, monkeypatch, guest_setup):
    """Call the public comment handler directly, with delivery stubbed out."""
    import app.services.notifications as notifications_module

    monkeypatch.setattr(
        "app.jobs.queue.enqueue_push_notification_job", lambda *a, **kw: True
    )
    monkeypatch.setattr(
        "app.jobs.queue.enqueue_comment_notification_email_job", lambda **kw: True
    )
    monkeypatch.setattr("app.jobs.queue.enqueue_mention_email_job", lambda **kw: True)

    async def _noop_send(user_id, event):  # noqa: ANN001
        return None

    monkeypatch.setattr(
        notifications_module.notifications_ws_manager, "send_to_user", _noop_send
    )

    from app.api.models.review_links import PublicReviewCommentCreate
    from app.api.routes.review_links import create_public_comment

    def _post(text: str = "The hook drags.", **overrides):
        body = PublicReviewCommentCreate(
            session_id=guest_setup["session"].id,
            text=text,
            timecode=overrides.pop("timecode", 7),
            **overrides,
        )
        return asyncio.run(
            create_public_comment(
                token=guest_setup["link"].token, body=body, db=db_session
            )
        )

    return _post


class GuestCommentNotificationTests:
    def test_a_plain_comment_notifies_the_owner_and_uploader(
        self, db_session, guest_setup, post_guest_comment
    ) -> None:
        post_guest_comment("The hook drags — can we tighten it?")

        rows = db_session.query(Notification).all()
        notified = {row.user_id for row in rows}

        assert notified == {guest_setup["owner"].id, guest_setup["uploader"].id}
        assert {row.type for row in rows} == {TYPE_CLIENT_COMMENT}

    def test_the_message_names_the_client_and_the_video(
        self, db_session, guest_setup, post_guest_comment
    ) -> None:
        post_guest_comment()

        message = db_session.query(Notification).first().message
        assert "Sarah Client" in message
        assert "Summer Reel" in message

    def test_a_change_request_says_so(
        self, db_session, guest_setup, post_guest_comment
    ) -> None:
        post_guest_comment("Replace the product shot.", kind="change_request")

        assert "requested a change" in db_session.query(Notification).first().message

    def test_the_notification_points_at_the_comment(
        self, db_session, guest_setup, post_guest_comment
    ) -> None:
        post_guest_comment()

        comment = db_session.query(Comment).one()
        row = db_session.query(Notification).first()
        assert row.comment_id == comment.id
        assert row.video_id == guest_setup["video"].id
        assert row.project_id == guest_setup["project"].id

    def test_a_review_session_of_comments_collapses_into_one_alert(
        self, db_session, guest_setup, post_guest_comment
    ) -> None:
        # The reason it is safe to notify at all: seven comments in one sitting
        # is one interruption, not seven.
        for i in range(7):
            post_guest_comment(f"Note {i}")

        rows = db_session.query(Notification).all()
        assert len(rows) == 2  # owner + uploader, one each
        assert {row.group_count for row in rows} == {7}

    def test_an_owner_who_also_uploaded_is_notified_once(
        self, db_session, make_user, make_project, make_video, monkeypatch
    ) -> None:
        import app.services.notifications as notifications_module

        monkeypatch.setattr(
            "app.jobs.queue.enqueue_push_notification_job", lambda *a, **kw: True
        )
        monkeypatch.setattr(
            "app.jobs.queue.enqueue_comment_notification_email_job", lambda **kw: True
        )

        async def _noop_send(user_id, event):  # noqa: ANN001
            return None

        monkeypatch.setattr(
            notifications_module.notifications_ws_manager, "send_to_user", _noop_send
        )

        solo = make_user(name="Solo Creator")
        project = make_project(creator=solo)
        video = make_video(project, uploader_id=solo.id)
        link = ReviewLink(video_id=video.id, token="solo-token", allow_comments=True)
        db_session.add(link)
        db_session.flush()
        session = ReviewSession(
            review_link_id=link.id, fingerprint="fp-solo", guest_name="Client"
        )
        db_session.add(session)
        db_session.commit()

        from app.api.models.review_links import PublicReviewCommentCreate
        from app.api.routes.review_links import create_public_comment

        asyncio.run(
            create_public_comment(
                token="solo-token",
                body=PublicReviewCommentCreate(
                    session_id=session.id, text="Looks great", timecode=1
                ),
                db=db_session,
            )
        )

        assert db_session.query(Notification).count() == 1

    def test_a_mention_is_not_duplicated_as_a_client_comment(
        self, db_session, guest_setup, post_guest_comment
    ) -> None:
        # The owner is mentioned by name; they should get one notification
        # about it, typed as a mention rather than a generic client comment.
        post_guest_comment("@Ollie Owner can you look at this?")

        rows = db_session.query(Notification).filter_by(
            user_id=guest_setup["owner"].id
        ).all()
        assert len(rows) == 1
        assert rows[0].type == TYPE_MENTION
