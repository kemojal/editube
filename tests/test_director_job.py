"""The director run as a state machine.

The row *is* the run's state, not a log of it, because the stages have wildly
different costs: planning is seconds and worth redoing, generating a dozen images
is minutes and money and is not. A run that dies mid-way has to be resumable
from where it stopped rather than restarted from the top.

What is tested here is the plumbing around the planner — the guards that stop a
run starting on a video with nothing to direct, the cancel that has to be honest
about work already paid for, and the failures that have to reach the row instead
of only the logs.
"""

from __future__ import annotations

import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import (
    AiResult,
    DirectorPlan,
    GeneratedMedia,
    Project,
    User,
    Video,
    VideoTranscription,
)
from app.jobs import director as job
from app.services import director_service


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(type_, compiler, **kw):
    return "JSON"


SEGMENTS = [
    {"start": 0.0, "end": 6.0, "text": "Most teams lose two days a week to meetings"},
    {"start": 6.0, "end": 14.0, "text": "and nobody can say where the time actually went"},
]

PLAN = {
    "version": 1,
    "brief": {"houseStylePrefix": "35mm, shallow depth of field."},
    "beats": [],
    "directives": [{"id": "d1", "why": "because"}],
    "warnings": ["Dropped one shot"],
    "usage": {"output_tokens": 900},
    "model": "claude-opus-5",
}


class _Run:
    """Stands in for a completed `director_service` run."""

    def __init__(self) -> None:
        self.plan = mock.Mock(directives=PLAN["directives"], warnings=PLAN["warnings"])
        self.usage = PLAN["usage"]
        self.model = PLAN["model"]

    def to_dict(self):
        return PLAN


class DirectorJobTests(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(
            engine,
            tables=[
                User.__table__, Project.__table__, Video.__table__,
                AiResult.__table__, VideoTranscription.__table__,
                DirectorPlan.__table__, GeneratedMedia.__table__,
            ],
        )
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()

        self.user = User(email="e@example.com", name="Edna", role="creator")
        self.db.add(self.user)
        self.db.flush()
        self.project = Project(name="Launch", creator_id=self.user.id, workspace_id=1)
        self.db.add(self.project)
        self.db.flush()
        self.video = Video(
            project_id=self.project.id, name="Interview", version=1,
            file_path="/media/interview.mp4", uploader_id=self.user.id, duration=14,
        )
        self.db.add(self.video)
        self.db.commit()

        patcher = mock.patch.object(job, "SessionLocal", self.Session)
        self.addCleanup(patcher.stop)
        patcher.start()

    def tearDown(self) -> None:
        self.db.close()

    def _transcribe(self, segments=SEGMENTS) -> None:
        self.db.add(
            VideoTranscription(video_id=self.video.id, status="completed", segments=segments)
        )
        self.db.commit()

    def _draft(self, data) -> None:
        self.db.add(
            AiResult(video_id=self.video.id, result_type="rough_cut_draft", result_data=data)
        )
        self.db.commit()

    def _plan_row(self, **kw) -> DirectorPlan:
        # `cancel_requested` and `allow_video` are set explicitly because
        # SQLite renders `server_default="false"` as the *string* `"false"`,
        # which is truthy in Python. Postgres has real booleans, so this is a
        # harness artefact rather than something the job has to defend against.
        defaults = {"cancel_requested": False, "allow_video": True, "progress": 0}
        defaults.update(kw)
        row = DirectorPlan(
            video_id=self.video.id, project_id=self.project.id,
            user_id=self.user.id, status="queued", **defaults,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def _run(self, row, side_effect=None, expected_error: str | None = None):
        with mock.patch.object(
            director_service, "generate_plan",
            side_effect=side_effect, return_value=None if side_effect else _Run(),
        ) as generate:
            if expected_error is None:
                result = job.director_job(row.id)
            else:
                with self.assertRaisesRegex(RuntimeError, expected_error) as caught:
                    job.director_job(row.id)
                result = caught.exception
        self.db.expire_all()
        return result, generate

    # -- guards --------------------------------------------------------------

    def test_a_video_with_no_transcript_fails_with_a_reason(self) -> None:
        """"Failed" with no reason sends people to the logs."""
        row = self._plan_row()
        _, generate = self._run(row, expected_error="no transcript")
        generate.assert_not_called()
        self.assertIn("no transcript", self.db.get(DirectorPlan, row.id).error_message)

    def test_a_transcript_with_no_speech_fails_before_calling_the_model(self) -> None:
        self._transcribe(segments=[{"start": 0.0, "end": 4.0, "text": "   "}])
        row = self._plan_row()
        _, generate = self._run(row, expected_error="no usable speech")
        generate.assert_not_called()

    def test_a_missing_plan_row_fails_the_rq_job_truthfully(self) -> None:
        """A vanished input is not a successful worker result."""
        with self.assertRaisesRegex(RuntimeError, "removed before processing"):
            job.director_job(999_999)

    # -- the happy path ------------------------------------------------------

    def test_a_successful_run_records_the_plan_and_what_it_cost(self) -> None:
        self._transcribe()
        row = self._plan_row()
        result, _ = self._run(row)

        self.assertEqual(result["status"], "ready")
        stored = self.db.get(DirectorPlan, row.id)
        self.assertEqual(stored.status, "ready")
        self.assertEqual(stored.plan, PLAN)
        self.assertEqual(stored.warnings, PLAN["warnings"])
        self.assertEqual(stored.usage, PLAN["usage"])
        self.assertEqual(stored.progress, 100)

    def test_the_users_options_reach_the_planner(self) -> None:
        self._transcribe()
        row = self._plan_row(tier="light", brief="No people.", allow_video=False)
        _, generate = self._run(row)

        options = generate.call_args.args[1]
        self.assertEqual(options.tier, "light")
        self.assertEqual(options.brief, "No people.")
        self.assertFalse(options.allow_video)

    def test_the_cut_is_read_from_the_draft_not_the_whole_take(self) -> None:
        """The director must only ever see the video that exists."""
        self._transcribe()
        self._draft({"keepRanges": [{"start": 0, "end": 6}], "sourceDuration": 14})
        row = self._plan_row()
        _, generate = self._run(row)

        context = generate.call_args.args[0]
        self.assertAlmostEqual(context.runtime_seconds, 6.0)
        self.assertNotIn("nobody can say", context.transcript)

    def test_the_target_aspect_is_taken_from_the_project(self) -> None:
        """A 16:9 still in a 9:16 export crops badly."""
        self._transcribe()
        self._draft({"layoutStyle": {"aspect": "9:16"}})
        row = self._plan_row()
        _, generate = self._run(row)
        self.assertEqual(generate.call_args.args[0].aspect, "9:16")

    def test_a_missing_video_duration_falls_back_to_the_draft(self) -> None:
        """The draft records the length it was actually cut against."""
        self.video.duration = None
        self._transcribe()
        self._draft({"sourceDuration": 14.0})
        self.db.commit()
        row = self._plan_row()
        _, generate = self._run(row)
        self.assertGreater(generate.call_args.args[0].runtime_seconds, 0)

    # -- cancellation --------------------------------------------------------

    def test_a_run_cancelled_before_it_starts_never_calls_the_model(self) -> None:
        self._transcribe()
        row = self._plan_row(cancel_requested=True)
        result, generate = self._run(row)
        self.assertEqual(result["status"], "cancelled")
        generate.assert_not_called()

    def test_a_cancel_during_planning_keeps_the_plan_it_paid_for(self) -> None:
        """The tokens are spent either way; throwing the plan away too is waste."""
        self._transcribe()
        row = self._plan_row()

        def cancel_midway(*_args, **_kw):
            other = self.Session()
            other.query(DirectorPlan).filter(DirectorPlan.id == row.id).update(
                {"cancel_requested": True}
            )
            other.commit()
            other.close()
            return _Run()

        result, _ = self._run(row, side_effect=cancel_midway)
        self.assertEqual(result["status"], "cancelled")
        stored = self.db.get(DirectorPlan, row.id)
        self.assertEqual(stored.status, "cancelled")
        self.assertEqual(stored.plan, PLAN)

    # -- failure -------------------------------------------------------------

    def test_an_unconfigured_deployment_says_so_on_the_row(self) -> None:
        self._transcribe()
        row = self._plan_row()
        _, _ = self._run(
            row,
            side_effect=director_service.DirectorUnavailable("ANTHROPIC_API_KEY is not set"),
            expected_error="ANTHROPIC_API_KEY",
        )
        self.assertIn("ANTHROPIC_API_KEY", self.db.get(DirectorPlan, row.id).error_message)

    def test_an_unusable_plan_is_reported_as_such(self) -> None:
        self._transcribe()
        row = self._plan_row()
        _, _ = self._run(
            row,
            side_effect=director_service.PlanRejected("Unsupported plan version 99"),
            expected_error="version 99",
        )
        self.assertIn("version 99", self.db.get(DirectorPlan, row.id).error_message)

    def test_an_unexpected_crash_still_reaches_the_row(self) -> None:
        """A run stuck at "planning" forever is worse than one marked failed."""
        self._transcribe()
        row = self._plan_row()
        _, _ = self._run(
            row,
            side_effect=RuntimeError("connection reset"),
            expected_error="connection reset",
        )
        stored = self.db.get(DirectorPlan, row.id)
        self.assertEqual(stored.status, "failed")
        self.assertIn("connection reset", stored.error_message)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
