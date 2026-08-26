"""Starting the director on its own, once the cut is ready.

The toggle is a one-shot consent, not a setting. The user asked for *this video*
to be directed, once, at the moment the cut became available — and the failure
mode this guards is a second run generating a second budget of images over a
timeline someone has since been editing. `auto-edit-gate.ts` learned the same
lesson for cuts; this is that lesson applied to something far more expensive.

The other property tested here is that the trigger cannot take transcription
down with it. By the time it runs, the transcript and the cut are finished and
are the valuable part of the job.
"""

from __future__ import annotations

import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import AiResult, DirectorPlan, Project, User, Video, VideoTranscription
from app.services import claude_client, director_trigger


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(type_, compiler, **kw):
    return "JSON"


SEGMENTS = [{"start": 0.0, "end": 4.0, "text": "Most teams lose two days a week"}]


class TriggerTests(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(
            engine,
            tables=[
                User.__table__, Project.__table__, Video.__table__,
                AiResult.__table__, VideoTranscription.__table__, DirectorPlan.__table__,
            ],
        )
        self.db = sessionmaker(bind=engine)()

        self.user = User(email="e@example.com", name="Edna", role="creator")
        self.db.add(self.user)
        self.db.flush()
        self.project = Project(name="Launch", creator_id=self.user.id, workspace_id=1)
        self.db.add(self.project)
        self.db.flush()
        self.video = Video(
            project_id=self.project.id, name="Interview", version=1,
            file_path="/m.mp4", uploader_id=self.user.id, duration=20,
        )
        self.db.add(self.video)
        self.db.flush()
        self.db.add(
            VideoTranscription(video_id=self.video.id, status="completed", segments=SEGMENTS)
        )
        self.db.commit()

        self.enqueued: list[int] = []
        patcher = mock.patch(
            "app.jobs.queue.enqueue_director_job",
            side_effect=lambda plan_id: self.enqueued.append(plan_id) or "job-1",
        )
        self.addCleanup(patcher.stop)
        patcher.start()

        available = mock.patch.object(claude_client, "available", return_value=True)
        self.addCleanup(available.stop)
        available.start()

    def tearDown(self) -> None:
        self.db.close()

    def _prefs(self, **overrides):
        data = director_trigger.default_prefs()
        data.update(overrides)
        row = AiResult(
            video_id=self.video.id, result_type=director_trigger.PREFS_TYPE, result_data=data
        )
        self.db.add(row)
        self.db.commit()
        return row

    def _run(self):
        result = director_trigger.run_post_cut_director(self.db, self.video.id)
        self.db.expire_all()
        return result

    def test_nothing_happens_without_a_preference_row(self) -> None:
        self.assertIsNone(self._run())
        self.assertEqual(self.enqueued, [])

    def test_nothing_happens_when_the_toggle_is_off(self) -> None:
        self._prefs(enabled=False)
        self.assertIsNone(self._run())

    def test_an_enabled_toggle_starts_a_run(self) -> None:
        self._prefs(enabled=True, tier="rich", brief="No people.", allow_video=False)
        plan_id = self._run()

        self.assertIsNotNone(plan_id)
        self.assertEqual(self.enqueued, [plan_id])
        plan = self.db.get(DirectorPlan, plan_id)
        self.assertEqual(plan.tier, "rich")
        self.assertEqual(plan.brief, "No people.")
        self.assertFalse(plan.allow_video)

    def test_the_consent_is_spent_so_a_retry_cannot_bill_twice(self) -> None:
        """Spent before enqueuing, so a crashed-and-retried transcription
        produces one run, not one per attempt."""
        prefs = self._prefs(enabled=True)
        self._run()
        self.db.refresh(prefs)
        self.assertFalse(prefs.result_data["enabled"])
        self.assertTrue(prefs.result_data["spent"])

    def test_running_twice_starts_only_one_run(self) -> None:
        """The failure this guard exists for: a second budget of images over a
        timeline someone has since been editing."""
        self._prefs(enabled=True)
        first = self._run()
        second = self._run()
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(self.enqueued), 1)

    def test_a_video_already_directed_is_not_directed_again(self) -> None:
        self.db.add(
            DirectorPlan(
                video_id=self.video.id, project_id=self.project.id, status="applied",
                tier="standard", allow_video=True, cancel_requested=False, progress=100,
            )
        )
        self.db.commit()
        self._prefs(enabled=True)
        self.assertIsNone(self._run())

    def test_a_video_with_no_transcript_starts_nothing(self) -> None:
        self.db.query(VideoTranscription).delete()
        self.db.commit()
        self._prefs(enabled=True)
        self.assertIsNone(self._run())

    def test_an_unconfigured_server_says_why_rather_than_going_quiet(self) -> None:
        """The user turned this on; they are entitled to know why nothing ran."""
        prefs = self._prefs(enabled=True)
        with mock.patch.object(claude_client, "available", return_value=False):
            self.assertIsNone(self._run())
        self.db.refresh(prefs)
        self.assertIn("not configured", prefs.result_data["skippedReason"])
        self.assertFalse(prefs.result_data["enabled"])

    def test_a_failure_never_propagates_to_the_transcription_job(self) -> None:
        """The transcript and the cut are done by now and are what matters."""
        self._prefs(enabled=True)
        with mock.patch(
            "app.jobs.queue.enqueue_director_job", side_effect=RuntimeError("redis is down")
        ):
            self.assertIsNone(self._run())

    def test_a_queue_that_is_not_configured_is_recorded_on_the_run(self) -> None:
        self._prefs(enabled=True)
        with mock.patch("app.jobs.queue.enqueue_director_job", return_value=None):
            plan_id = self._run()
        plan = self.db.get(DirectorPlan, plan_id)
        self.assertEqual(plan.status, "failed")
        self.assertIn("REDIS_URL", plan.error_message)


class PipelineOrderTests(unittest.TestCase):
    def test_the_director_runs_after_the_auto_edit_seeds_the_cut(self) -> None:
        """Order matters: the director must read the video as it will be
        watched, not the uncut take."""
        source = (
            __import__("pathlib")
            .Path(__import__("app.jobs.transcription", fromlist=["x"]).__file__)
            .read_text()
        )
        self.assertLess(
            source.index("run_post_transcription_auto_edit("),
            source.index("run_post_cut_director("),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
