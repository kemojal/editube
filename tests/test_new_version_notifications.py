"""Uploading a new version must tell the people invested in the old one.

`TYPE_NEW_VERSION` existed — push titles, frontend labels, deep links — but no
code path ever emitted it, so reviewers discovered new cuts by accident.
"""

from __future__ import annotations

from app.api.routes.videos import _finalize_project_video
from app.db.models import Notification


class NewVersionNotificationTests:
    def _upload_next_version(self, db_session, uploader, project, base_video):
        return _finalize_project_video(
            db_session,
            project_id=project.id,
            current_user=uploader,
            file_path="https://cdn.example.test/v2.mp4",
            size_bytes=10,
            name=base_video.name,
            description=None,
            folder_id=None,
            version=(base_video.version or 1) + 1,
            version_group_id=base_video.version_group_id,
            language=None,
            activity_action="video_uploaded",
            base_video=base_video,
        )

    def test_commenters_and_owner_hear_about_the_new_cut(
        self, db_session, make_user, make_project, make_video, make_comment
    ) -> None:
        owner = make_user(name="Ollie Owner")
        editor = make_user(name="Ed Editor")
        commenter = make_user(name="Cara Commenter")
        project = make_project(creator=owner)
        v1 = make_video(project, version=1, uploader_id=editor.id)
        make_comment(v1, user_id=commenter.id)
        db_session.commit()

        self._upload_next_version(db_session, editor, project, v1)

        rows = db_session.query(Notification).filter_by(type="new_version").all()
        assert {row.user_id for row in rows} == {owner.id, commenter.id}
        assert all("uploaded v2" in row.message for row in rows)

    def test_the_uploader_is_not_notified_about_their_own_upload(
        self, db_session, make_user, make_project, make_video, make_comment
    ) -> None:
        solo = make_user(name="Solo")
        project = make_project(creator=solo)
        v1 = make_video(project, version=1, uploader_id=solo.id)
        make_comment(v1, user_id=solo.id)
        db_session.commit()

        self._upload_next_version(db_session, solo, project, v1)

        assert db_session.query(Notification).count() == 0

    def test_assignees_count_as_invested(
        self, db_session, make_user, make_project, make_video, make_comment
    ) -> None:
        owner = make_user()
        assignee = make_user(name="Amy Assignee")
        project = make_project(creator=owner)
        v1 = make_video(project, version=1, uploader_id=owner.id)
        make_comment(v1, user_id=owner.id, assignee_user_id=assignee.id)
        db_session.commit()

        self._upload_next_version(db_session, owner, project, v1)

        notified = {
            row.user_id
            for row in db_session.query(Notification).filter_by(type="new_version")
        }
        assert assignee.id in notified

    def test_a_fresh_upload_notifies_nobody(
        self, db_session, make_user, make_project
    ) -> None:
        # No base video means no previous audience to inform.
        user = make_user()
        project = make_project(creator=user)
        db_session.commit()

        _finalize_project_video(
            db_session,
            project_id=project.id,
            current_user=user,
            file_path="https://cdn.example.test/v1.mp4",
            size_bytes=10,
            name="Fresh",
            description=None,
            folder_id=None,
            version=1,
            version_group_id="grp-fresh",
            language=None,
            activity_action="video_uploaded",
        )

        assert db_session.query(Notification).count() == 0
