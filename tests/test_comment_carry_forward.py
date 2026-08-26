"""Carrying an editor's punch list onto the next version.

Before this, every unresolved change request became read-only history the
moment v2 landed — the checklist evaporated at exactly the point the editor sat
down to work through it.
"""

from __future__ import annotations

from app.db.models import Comment
from app.services.comment_carry_forward import (
    carry_forward_open_change_requests,
    count_open_change_requests,
    open_change_requests,
)


class SelectionTests:
    def test_only_change_requests_carry(self, db_session, make_video, make_comment) -> None:
        video = make_video()
        make_comment(video, kind="change_request", status="open", text="Fix the logo")
        make_comment(video, kind="comment", status="open", text="Nice transition")
        db_session.commit()

        assert [c.text for c in open_change_requests(db_session, video.id)] == [
            "Fix the logo"
        ]

    def test_closed_requests_stay_behind(
        self, db_session, make_video, make_comment
    ) -> None:
        video = make_video()
        make_comment(video, kind="change_request", status="resolved")
        make_comment(video, kind="change_request", status="wontfix")
        make_comment(video, kind="change_request", status="open")
        make_comment(video, kind="change_request", status="in_progress")
        db_session.commit()

        # open and in_progress are both live work.
        assert count_open_change_requests(db_session, video.id) == 2

    def test_replies_do_not_carry(self, db_session, make_video, make_comment) -> None:
        # A thread belongs to the version where the conversation happened.
        video = make_video()
        parent = make_comment(video, kind="change_request", status="open")
        make_comment(video, kind="change_request", status="open", parent_id=parent.id)
        db_session.commit()

        assert len(open_change_requests(db_session, video.id)) == 1

    def test_ordered_by_timecode(self, db_session, make_video, make_comment) -> None:
        # An editor works the timeline in order, not in comment-creation order.
        video = make_video()
        make_comment(video, kind="change_request", status="open", timecode=30, text="c")
        make_comment(video, kind="change_request", status="open", timecode=5, text="a")
        make_comment(video, kind="change_request", status="open", timecode=12, text="b")
        db_session.commit()

        assert [c.text for c in open_change_requests(db_session, video.id)] == [
            "a",
            "b",
            "c",
        ]


class CarryForwardTests:
    def test_copies_open_requests_onto_the_new_version(
        self, db_session, make_project, make_video, make_comment
    ) -> None:
        project = make_project()
        v1 = make_video(project, version=1, version_group_id="grp")
        v2 = make_video(project, version=2, version_group_id="grp")
        make_comment(v1, kind="change_request", status="open", text="Trim the intro")
        db_session.commit()

        copies = carry_forward_open_change_requests(db_session, v1, v2)
        db_session.commit()

        assert len(copies) == 1
        assert copies[0].video_id == v2.id
        assert copies[0].text == "Trim the intro"

    def test_the_original_stays_put(
        self, db_session, make_project, make_video, make_comment
    ) -> None:
        # Carry-forward copies; it never moves. The history view depends on it.
        project = make_project()
        v1 = make_video(project, version=1, version_group_id="grp")
        v2 = make_video(project, version=2, version_group_id="grp")
        original = make_comment(v1, kind="change_request", status="open")
        db_session.commit()

        carry_forward_open_change_requests(db_session, v1, v2)
        db_session.commit()

        db_session.refresh(original)
        assert original.video_id == v1.id
        assert db_session.query(Comment).filter_by(video_id=v1.id).count() == 1

    def test_the_copy_points_home(
        self, db_session, make_project, make_video, make_comment
    ) -> None:
        project = make_project()
        v1 = make_video(project, version=1, version_group_id="grp")
        v2 = make_video(project, version=2, version_group_id="grp")
        original = make_comment(v1, kind="change_request", status="open")
        db_session.commit()

        copy = carry_forward_open_change_requests(db_session, v1, v2)[0]
        db_session.commit()

        assert copy.carried_from_comment_id == original.id

    def test_workflow_fields_survive(
        self, db_session, make_project, make_video, make_comment, make_user
    ) -> None:
        project = make_project()
        assignee = make_user()
        v1 = make_video(project, version=1, version_group_id="grp")
        v2 = make_video(project, version=2, version_group_id="grp")
        make_comment(
            v1,
            kind="change_request",
            status="in_progress",
            timecode=42,
            end_timecode=48,
            visibility="team",
            assignee_user_id=assignee.id,
        )
        db_session.commit()

        copy = carry_forward_open_change_requests(db_session, v1, v2)[0]

        assert copy.timecode == 42
        assert copy.end_timecode == 48
        assert copy.visibility == "team"
        assert copy.assignee_user_id == assignee.id
        # Reopened against the new cut: "in progress" on a version that no
        # longer exists would be a lie.
        assert copy.status == "open"
        assert copy.is_resolved is False

    def test_guest_authorship_survives(
        self, db_session, make_project, make_video
    ) -> None:
        # The client's name stays on their own request, rather than the
        # request appearing to be the editor's.
        project = make_project()
        v1 = make_video(project, version=1, version_group_id="grp")
        v2 = make_video(project, version=2, version_group_id="grp")
        db_session.add(
            Comment(
                video_id=v1.id,
                user_id=None,
                text="Swap the music",
                timecode=3,
                kind="change_request",
                status="open",
                visibility="public",
                guest_name="Sarah Client",
                guest_email="sarah@client.test",
            )
        )
        db_session.commit()

        copy = carry_forward_open_change_requests(db_session, v1, v2)[0]

        assert copy.guest_name == "Sarah Client"
        assert copy.guest_email == "sarah@client.test"
        assert copy.user_id is None

    def test_transcript_word_indices_are_dropped(
        self, db_session, make_project, make_video, make_comment
    ) -> None:
        # They point into the old cut's transcript; anchor_text survives so the
        # existing remap logic can re-resolve or flag drift.
        project = make_project()
        v1 = make_video(project, version=1, version_group_id="grp")
        v2 = make_video(project, version=2, version_group_id="grp")
        make_comment(
            v1,
            kind="change_request",
            status="open",
            transcript_segment_index=4,
            word_start_index=22,
            word_end_index=28,
            anchor_text="quickly from their words",
        )
        db_session.commit()

        copy = carry_forward_open_change_requests(db_session, v1, v2)[0]

        assert copy.anchor_text == "quickly from their words"
        assert copy.word_start_index is None
        assert copy.transcript_segment_index is None

    def test_running_twice_does_not_duplicate(
        self, db_session, make_project, make_video, make_comment
    ) -> None:
        project = make_project()
        v1 = make_video(project, version=1, version_group_id="grp")
        v2 = make_video(project, version=2, version_group_id="grp")
        make_comment(v1, kind="change_request", status="open")
        db_session.commit()

        carry_forward_open_change_requests(db_session, v1, v2)
        db_session.commit()
        second = carry_forward_open_change_requests(db_session, v1, v2)
        db_session.commit()

        assert second == []
        assert db_session.query(Comment).filter_by(video_id=v2.id).count() == 1

    def test_nothing_open_is_a_no_op(
        self, db_session, make_project, make_video, make_comment
    ) -> None:
        project = make_project()
        v1 = make_video(project, version=1, version_group_id="grp")
        v2 = make_video(project, version=2, version_group_id="grp")
        make_comment(v1, kind="change_request", status="resolved")
        db_session.commit()

        assert carry_forward_open_change_requests(db_session, v1, v2) == []

    def test_carrying_onto_itself_is_refused(
        self, db_session, make_video, make_comment
    ) -> None:
        video = make_video()
        make_comment(video, kind="change_request", status="open")
        db_session.commit()

        assert carry_forward_open_change_requests(db_session, video, video) == []
