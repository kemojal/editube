"""`app/services/notifications.py` — the single notification emitter.

The behaviour that matters here is coalescing: it is the reason it becomes
safe to notify editors about every guest comment, which is the gap that let
client feedback rot silently.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db.models import Notification
from app.services.notifications import (
    GROUP_WINDOW_MINUTES,
    TYPE_CLIENT_COMMENT,
    TYPE_VIDEO_APPROVED,
    NotificationSpec,
    build_notifications,
    emit_notifications,
)


class BuildTests:
    def test_creates_a_row_per_spec(self, db_session, make_user, make_video) -> None:
        video = make_video()
        users = [make_user(), make_user()]

        rows = build_notifications(
            db_session,
            [
                NotificationSpec(user_id=u.id, type=TYPE_CLIENT_COMMENT, video_id=video.id)
                for u in users
            ],
        )
        db_session.commit()

        assert len(rows) == 2
        assert db_session.query(Notification).count() == 2

    def test_records_the_actor_and_message(self, db_session, make_user, make_video) -> None:
        recipient, actor = make_user(), make_user(name="Sarah Client")
        video = make_video()

        build_notifications(
            db_session,
            [
                NotificationSpec(
                    user_id=recipient.id,
                    type=TYPE_CLIENT_COMMENT,
                    video_id=video.id,
                    actor_user_id=actor.id,
                    message="Sarah Client left a comment",
                )
            ],
        )
        db_session.commit()

        row = db_session.query(Notification).one()
        assert row.actor_user_id == actor.id
        assert row.message == "Sarah Client left a comment"
        assert row.group_count == 1

    def test_specs_without_a_recipient_are_skipped(self, db_session) -> None:
        rows = build_notifications(
            db_session, [NotificationSpec(user_id=0, type=TYPE_CLIENT_COMMENT)]
        )
        assert rows == []


class CoalescingTests:
    def test_siblings_fold_into_one_row(self, db_session, make_user, make_video) -> None:
        user, video = make_user(), make_video()
        spec = NotificationSpec(
            user_id=user.id,
            type=TYPE_CLIENT_COMMENT,
            video_id=video.id,
            group_key=f"client_comment:{video.id}:session-1",
        )

        for _ in range(7):
            build_notifications(db_session, [spec])
            db_session.commit()

        rows = db_session.query(Notification).all()
        assert len(rows) == 1
        assert rows[0].group_count == 7

    def test_different_keys_do_not_fold(self, db_session, make_user, make_video) -> None:
        user, video = make_user(), make_video()

        for session_id in ("session-1", "session-2"):
            build_notifications(
                db_session,
                [
                    NotificationSpec(
                        user_id=user.id,
                        type=TYPE_CLIENT_COMMENT,
                        video_id=video.id,
                        group_key=f"client_comment:{video.id}:{session_id}",
                    )
                ],
            )
            db_session.commit()

        assert db_session.query(Notification).count() == 2

    def test_no_key_means_always_insert(self, db_session, make_user, make_video) -> None:
        user, video = make_user(), make_video()
        spec = NotificationSpec(
            user_id=user.id, type=TYPE_CLIENT_COMMENT, video_id=video.id
        )

        build_notifications(db_session, [spec])
        build_notifications(db_session, [spec])
        db_session.commit()

        assert db_session.query(Notification).count() == 2

    def test_a_read_notification_is_not_reused(
        self, db_session, make_user, make_video
    ) -> None:
        user, video = make_user(), make_video()
        spec = NotificationSpec(
            user_id=user.id,
            type=TYPE_CLIENT_COMMENT,
            video_id=video.id,
            group_key="k",
        )
        first = build_notifications(db_session, [spec])[0]
        db_session.commit()
        first.read = True
        db_session.commit()

        build_notifications(db_session, [spec])
        db_session.commit()

        # Folding into an already-read row would resurrect a notification the
        # user has dealt with.
        assert db_session.query(Notification).count() == 2

    def test_stale_notifications_fall_outside_the_window(
        self, db_session, make_user, make_video
    ) -> None:
        user, video = make_user(), make_video()
        spec = NotificationSpec(
            user_id=user.id,
            type=TYPE_CLIENT_COMMENT,
            video_id=video.id,
            group_key="k",
        )
        first = build_notifications(db_session, [spec])[0]
        db_session.commit()
        first.created_at = datetime.now(timezone.utc) - timedelta(
            minutes=GROUP_WINDOW_MINUTES + 1
        )
        db_session.commit()

        build_notifications(db_session, [spec])
        db_session.commit()

        assert db_session.query(Notification).count() == 2

    def test_decisions_never_group_even_with_a_key(
        self, db_session, make_user, make_video
    ) -> None:
        # "Approved" and "changes requested" are the answers everyone is
        # waiting on; burying the second one inside a batch would be a bug.
        user, video = make_user(), make_video()
        spec = NotificationSpec(
            user_id=user.id,
            type=TYPE_VIDEO_APPROVED,
            video_id=video.id,
            group_key=f"decision:{video.id}",
        )

        build_notifications(db_session, [spec])
        build_notifications(db_session, [spec])
        db_session.commit()

        assert db_session.query(Notification).count() == 2


def _run(coro):
    """Drive a coroutine from a sync test.

    Matches the convention already in `tests/test_video_stream_refresh.py`
    rather than adding pytest-asyncio to the environment.
    """
    import asyncio

    return asyncio.run(coro)


class EmitTests:
    def test_emit_commits_and_delivers(
        self, db_session, make_user, make_video, monkeypatch
    ) -> None:
        pushed: list[tuple[int, int]] = []
        sent: list[dict] = []

        import app.services.notifications as notifications_module

        monkeypatch.setattr(
            "app.jobs.queue.enqueue_push_notification_job",
            lambda user_id, notification_id: pushed.append((user_id, notification_id)),
        )

        async def _fake_send(user_id, event):  # noqa: ANN001
            sent.append(event)

        monkeypatch.setattr(
            notifications_module.notifications_ws_manager, "send_to_user", _fake_send
        )

        user, video = make_user(), make_video()
        rows = _run(
            emit_notifications(
                db_session,
                [
                    NotificationSpec(
                        user_id=user.id, type=TYPE_CLIENT_COMMENT, video_id=video.id
                    )
                ],
            )
        )

        assert len(rows) == 1
        assert pushed == [(user.id, rows[0].id)]
        assert sent[0]["event"] == "notification.new"
        assert sent[0]["payload"]["video_id"] == video.id

    def test_delivery_failure_does_not_lose_the_row(
        self, db_session, make_user, make_video, monkeypatch
    ) -> None:
        import app.services.notifications as notifications_module

        monkeypatch.setattr(
            "app.jobs.queue.enqueue_push_notification_job",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("redis down")),
        )

        async def _boom(user_id, event):  # noqa: ANN001
            raise RuntimeError("socket closed")

        monkeypatch.setattr(
            notifications_module.notifications_ws_manager, "send_to_user", _boom
        )

        user, video = make_user(), make_video()
        _run(
            emit_notifications(
                db_session,
                [
                    NotificationSpec(
                        user_id=user.id, type=TYPE_CLIENT_COMMENT, video_id=video.id
                    )
                ],
            )
        )

        assert db_session.query(Notification).count() == 1

    def test_no_specs_is_a_no_op(self, db_session) -> None:
        assert _run(emit_notifications(db_session, [])) == []
