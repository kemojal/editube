"""`app/services/video_status.py` — the single writer of `Video.status`.

Covers the rules that did not exist when status was assigned inline in two
route handlers: that illegal moves are refused, that provenance is recorded,
and that a decision and the status it implies can never drift apart.
"""

from __future__ import annotations

import pytest

from app.db.models import ActivityFeed, VideoApproval
from app.services.video_status import (
    DECISION_APPROVED,
    DECISION_CHANGES_REQUESTED,
    STATUS_APPROVED,
    STATUS_IN_PROGRESS,
    STATUS_IN_REVIEW,
    STATUS_NEEDS_CHANGES,
    IllegalStatusTransition,
    InvalidVideoStatus,
    apply_video_status,
    assert_transition,
    decision_summary,
    record_decision,
    supersede_open_decisions,
)


class TransitionRuleTests:
    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (STATUS_IN_PROGRESS, STATUS_IN_REVIEW),
            (STATUS_IN_REVIEW, STATUS_APPROVED),
            (STATUS_IN_REVIEW, STATUS_NEEDS_CHANGES),
            (STATUS_IN_REVIEW, STATUS_IN_PROGRESS),
            (STATUS_NEEDS_CHANGES, STATUS_IN_REVIEW),
            # A client changing their mind after sign-off is a real workflow,
            # not an error state.
            (STATUS_APPROVED, STATUS_NEEDS_CHANGES),
            (STATUS_APPROVED, STATUS_IN_REVIEW),
        ],
    )
    def test_legal_moves_are_allowed(self, current, target) -> None:
        assert_transition(current, target)

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            # The whole point: a cut cannot be approved without ever having
            # been sent for review.
            (STATUS_IN_PROGRESS, STATUS_APPROVED),
            (STATUS_IN_PROGRESS, STATUS_NEEDS_CHANGES),
            (STATUS_NEEDS_CHANGES, STATUS_APPROVED),
        ],
    )
    def test_illegal_moves_are_refused(self, current, target) -> None:
        with pytest.raises(IllegalStatusTransition):
            assert_transition(current, target)

    def test_staying_put_is_always_allowed(self) -> None:
        for status in (STATUS_IN_PROGRESS, STATUS_IN_REVIEW, STATUS_APPROVED):
            assert_transition(status, status)

    def test_missing_current_status_is_treated_as_in_progress(self) -> None:
        assert_transition(None, STATUS_IN_REVIEW)
        with pytest.raises(IllegalStatusTransition):
            assert_transition(None, STATUS_APPROVED)

    def test_unknown_status_is_rejected_before_the_transition_check(self) -> None:
        with pytest.raises(InvalidVideoStatus):
            assert_transition(STATUS_IN_REVIEW, "published")

    def test_the_error_names_what_is_allowed_instead(self) -> None:
        with pytest.raises(IllegalStatusTransition) as excinfo:
            assert_transition(STATUS_IN_PROGRESS, STATUS_APPROVED)
        assert STATUS_IN_REVIEW in str(excinfo.value)


class ApplyStatusTests:
    def test_records_who_changed_it_and_when(self, db_session, make_video, make_user) -> None:
        actor = make_user()
        video = make_video()

        apply_video_status(db_session, video, STATUS_IN_REVIEW, actor_user_id=actor.id)

        assert video.status == STATUS_IN_REVIEW
        assert video.status_changed_by == actor.id
        assert video.status_changed_at is not None

    def test_logs_an_activity_entry(self, db_session, make_video, make_user) -> None:
        actor = make_user()
        video = make_video()

        apply_video_status(db_session, video, STATUS_IN_REVIEW, actor_user_id=actor.id)
        db_session.commit()

        entry = (
            db_session.query(ActivityFeed)
            .filter(ActivityFeed.action == "video_status_changed")
            .one()
        )
        assert entry.project_id == video.project_id
        assert entry.user_id == actor.id

    def test_a_no_op_move_does_not_restamp_provenance(
        self, db_session, make_video, make_user
    ) -> None:
        video = make_video(status=STATUS_IN_REVIEW)

        apply_video_status(db_session, video, STATUS_IN_REVIEW, actor_user_id=make_user().id)

        assert video.status_changed_at is None

    def test_illegal_move_leaves_the_video_untouched(
        self, db_session, make_video, make_user
    ) -> None:
        video = make_video(status=STATUS_IN_PROGRESS)

        with pytest.raises(IllegalStatusTransition):
            apply_video_status(
                db_session, video, STATUS_APPROVED, actor_user_id=make_user().id
            )

        assert video.status == STATUS_IN_PROGRESS
        assert video.status_changed_at is None

    def test_skip_transition_check_forces_the_move(
        self, db_session, make_video, make_user
    ) -> None:
        # Used when a new version lands and resets the cut to in_review.
        video = make_video(status=STATUS_IN_PROGRESS)

        apply_video_status(
            db_session,
            video,
            STATUS_APPROVED,
            actor_user_id=make_user().id,
            skip_transition_check=True,
        )

        assert video.status == STATUS_APPROVED


class RecordDecisionTests:
    def test_approval_moves_the_video_to_approved(
        self, db_session, make_video, make_user
    ) -> None:
        actor = make_user()
        video = make_video(status=STATUS_IN_REVIEW)

        approval = record_decision(
            db_session, video, DECISION_APPROVED, actor_user_id=actor.id
        )

        assert video.status == STATUS_APPROVED
        assert approval.decision == DECISION_APPROVED
        assert approval.video_id == video.id

    def test_changes_requested_moves_the_video_to_needs_changes(
        self, db_session, make_video, make_user
    ) -> None:
        video = make_video(status=STATUS_IN_REVIEW)

        record_decision(
            db_session,
            video,
            DECISION_CHANGES_REQUESTED,
            actor_user_id=make_user().id,
            note="Trim the intro.",
        )

        assert video.status == STATUS_NEEDS_CHANGES

    def test_a_guest_decision_carries_the_session_not_a_user(
        self, db_session, make_video
    ) -> None:
        video = make_video(status=STATUS_IN_REVIEW)

        approval = record_decision(
            db_session, video, DECISION_APPROVED, review_session_id=42, review_link_id=7
        )

        assert approval.actor_user_id is None
        assert approval.review_session_id == 42
        assert approval.review_link_id == 7

    def test_a_guest_can_approve_a_cut_that_was_never_formally_sent(
        self, db_session, make_video
    ) -> None:
        # in_progress -> approved is not a legal manual transition, but a guest
        # handed a link should never be stranded by our bookkeeping.
        video = make_video(status=STATUS_IN_PROGRESS)

        record_decision(db_session, video, DECISION_APPROVED, review_session_id=1)

        assert video.status == STATUS_APPROVED

    def test_an_unknown_decision_is_rejected(self, db_session, make_video) -> None:
        with pytest.raises(ValueError):
            record_decision(db_session, make_video(), "maybe")

    def test_decisions_accumulate_rather_than_overwrite(
        self, db_session, make_video, make_user
    ) -> None:
        actor = make_user()
        video = make_video(status=STATUS_IN_REVIEW)

        record_decision(db_session, video, DECISION_CHANGES_REQUESTED, actor_user_id=actor.id)
        record_decision(db_session, video, DECISION_APPROVED, actor_user_id=actor.id)
        db_session.commit()

        assert db_session.query(VideoApproval).filter_by(video_id=video.id).count() == 2
        assert video.status == STATUS_APPROVED


class SupersedeTests:
    def test_a_new_version_supersedes_live_decisions(
        self, db_session, make_project, make_video, make_user
    ) -> None:
        project = make_project()
        actor = make_user()
        v1 = make_video(project, version=1, status=STATUS_IN_REVIEW)
        v2 = make_video(project, version=2)
        record_decision(db_session, v1, DECISION_APPROVED, actor_user_id=actor.id)

        count = supersede_open_decisions(
            db_session, [v1.id], superseded_by_video_id=v2.id
        )
        db_session.commit()

        assert count == 1
        approval = db_session.query(VideoApproval).filter_by(video_id=v1.id).one()
        assert approval.superseded_at is not None
        assert approval.superseded_by_video_id == v2.id

    def test_already_superseded_decisions_are_not_restamped(
        self, db_session, make_project, make_video, make_user
    ) -> None:
        project = make_project()
        v1 = make_video(project, version=1, status=STATUS_IN_REVIEW)
        v2 = make_video(project, version=2)
        v3 = make_video(project, version=3)
        record_decision(db_session, v1, DECISION_APPROVED, actor_user_id=make_user().id)
        supersede_open_decisions(db_session, [v1.id], superseded_by_video_id=v2.id)

        second = supersede_open_decisions(db_session, [v1.id], superseded_by_video_id=v3.id)

        assert second == 0
        approval = db_session.query(VideoApproval).filter_by(video_id=v1.id).one()
        assert approval.superseded_by_video_id == v2.id

    def test_empty_input_is_a_no_op(self, db_session) -> None:
        assert supersede_open_decisions(db_session, [], superseded_by_video_id=1) == 0


class DecisionSummaryTests:
    def test_none_when_never_decided(self, db_session, make_video) -> None:
        assert decision_summary(db_session, make_video()) is None

    def test_reports_the_latest_decision_with_the_actors_name(
        self, db_session, make_video, make_user
    ) -> None:
        actor = make_user(name="Dana Director")
        video = make_video(status=STATUS_IN_REVIEW)
        record_decision(db_session, video, DECISION_CHANGES_REQUESTED, actor_user_id=actor.id)
        record_decision(
            db_session, video, DECISION_APPROVED, actor_user_id=actor.id, note="Ship it."
        )
        db_session.commit()

        summary = decision_summary(db_session, video)

        assert summary["decision"] == DECISION_APPROVED
        assert summary["actor_name"] == "Dana Director"
        assert summary["note"] == "Ship it."
        assert summary["superseded"] is False

    def test_flags_a_superseded_decision(
        self, db_session, make_project, make_video, make_user
    ) -> None:
        project = make_project()
        v1 = make_video(project, version=1, status=STATUS_IN_REVIEW)
        v2 = make_video(project, version=2)
        record_decision(db_session, v1, DECISION_APPROVED, actor_user_id=make_user().id)
        supersede_open_decisions(db_session, [v1.id], superseded_by_video_id=v2.id)
        db_session.commit()

        assert decision_summary(db_session, v1)["superseded"] is True


class LegacyStatusTests:
    """Real rows carry values the current vocabulary never defined.

    Two videos in production hold `'ready'` from an older upload pipeline.
    Because that value has no entry in ALLOWED_TRANSITIONS, every move from it
    raised — those cuts could never be sent for review at all.
    """

    def test_unknown_values_read_as_in_progress(self) -> None:
        from app.services.video_status import normalize_status

        assert normalize_status("ready") == STATUS_IN_PROGRESS
        assert normalize_status("published") == STATUS_IN_PROGRESS
        assert normalize_status(None) == STATUS_IN_PROGRESS
        assert normalize_status("") == STATUS_IN_PROGRESS

    def test_known_values_pass_through(self) -> None:
        from app.services.video_status import normalize_status

        for status in (STATUS_IN_PROGRESS, STATUS_IN_REVIEW, STATUS_APPROVED, STATUS_NEEDS_CHANGES):
            assert normalize_status(status) == status

    def test_a_legacy_cut_can_be_sent_for_review(self, db_session, make_video, make_user) -> None:
        video = make_video(status="ready")

        apply_video_status(
            db_session, video, STATUS_IN_REVIEW, actor_user_id=make_user().id
        )

        assert video.status == STATUS_IN_REVIEW

    def test_a_legacy_cut_still_cannot_skip_review(self, db_session, make_video, make_user) -> None:
        # Normalizing must not weaken the rules — 'ready' behaves exactly like
        # in_progress, which cannot jump straight to approved.
        video = make_video(status="ready")

        with pytest.raises(IllegalStatusTransition):
            apply_video_status(
                db_session, video, STATUS_APPROVED, actor_user_id=make_user().id
            )
