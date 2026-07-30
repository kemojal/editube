"""Reconciling effect rows whose worker died.

Without this a job whose work-horse was killed stays `processing` forever and the
editor polls a percentage that will never move again. That is not hypothetical:
three rows sat at 8% indefinitely after the work-horse took a signal, because RQ
marked *its* job failed and nothing told our row.

Keyed on the RQ job's real state rather than elapsed time, so a slow job is never
declared dead for being slow.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from app.api.routes.ai import _reconcile_dead_effect


def row(status="processing", *, rq_job_id="job-1", progress=8, clip_key="clip-a"):
    return SimpleNamespace(
        id=7,
        video_id=43,
        status=status,
        error_message=None,
        result_data={
            "status": status,
            "progress": progress,
            "rqJobId": rq_job_id,
            "clipKey": clip_key,
            "effectType": "remove_bg",
        },
    )


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.db = mock.MagicMock()
        # Silence the draft write; it needs a real session otherwise.
        patcher = mock.patch("app.jobs.rough_cut_effect._attach_to_draft")
        self.attach = patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict("os.environ", {"REDIS_URL": "redis://127.0.0.1:6379/0"})
        env.start()
        self.addCleanup(env.stop)

    def _fetch(self, job):
        """Patches Job.fetch to return `job`, or raise if it is an Exception."""

        def fetch(_id, connection=None):
            if isinstance(job, Exception):
                raise job
            return job

        return mock.patch("rq.job.Job.fetch", side_effect=fetch)

    def test_a_completed_row_is_left_alone(self):
        target = row("completed")
        _reconcile_dead_effect(self.db, target)
        self.assertEqual(target.status, "completed")
        self.db.commit.assert_not_called()

    def test_a_failed_row_is_left_alone(self):
        target = row("failed")
        _reconcile_dead_effect(self.db, target)
        self.db.commit.assert_not_called()

    def test_a_running_job_is_left_alone(self):
        # The important negative: a slow job must not be killed for being slow.
        job = mock.MagicMock()
        job.get_status.return_value = "started"
        target = row("processing")
        with self._fetch(job):
            _reconcile_dead_effect(self.db, target)
        self.assertEqual(target.status, "processing")
        self.db.commit.assert_not_called()

    def test_a_queued_job_is_left_alone(self):
        job = mock.MagicMock()
        job.get_status.return_value = "queued"
        target = row("queued")
        with self._fetch(job):
            _reconcile_dead_effect(self.db, target)
        self.assertEqual(target.status, "queued")

    def test_a_failed_rq_job_marks_the_row_failed(self):
        job = mock.MagicMock()
        job.get_status.return_value = "failed"
        job.exc_info = "Traceback...\nWork-horse terminated unexpectedly; signal 6"
        target = row("processing")
        with self._fetch(job):
            _reconcile_dead_effect(self.db, target)
        self.assertEqual(target.status, "failed")
        self.assertIn("worker stopped", target.error_message)
        # The underlying reason is the actionable part, so it must survive.
        self.assertIn("signal 6", target.error_message)
        self.assertEqual(target.result_data["progress"], 0)

    def test_a_vanished_job_marks_the_row_failed(self):
        # Job.fetch raises once the job has expired out of Redis.
        target = row("processing")
        with self._fetch(RuntimeError("NoSuchJobError")):
            _reconcile_dead_effect(self.db, target)
        self.assertEqual(target.status, "failed")

    def test_a_canceled_job_marks_the_row_failed(self):
        for status in ("canceled", "stopped"):
            with self.subTest(status=status):
                job = mock.MagicMock()
                job.get_status.return_value = status
                job.exc_info = None
                target = row("processing")
                with self._fetch(job):
                    _reconcile_dead_effect(self.db, target)
                self.assertEqual(target.status, "failed")

    def test_the_draft_is_updated_so_the_inspector_stops_spinning(self):
        job = mock.MagicMock()
        job.get_status.return_value = "failed"
        job.exc_info = None
        target = row("processing")
        with self._fetch(job):
            _reconcile_dead_effect(self.db, target)
        self.attach.assert_called_once()
        payload = self.attach.call_args[0][4]
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["progress"], 0)

    def test_a_row_with_no_rq_job_id_is_left_alone(self):
        # Nothing to check against, so guessing would be worse than waiting.
        target = row("processing", rq_job_id="")
        _reconcile_dead_effect(self.db, target)
        self.assertEqual(target.status, "processing")

    def test_redis_being_unreachable_leaves_the_row_alone(self):
        # A wrong "failed" is worse than a stale "processing".
        target = row("processing")
        with mock.patch("redis.Redis.from_url", side_effect=OSError("no redis")):
            _reconcile_dead_effect(self.db, target)
        self.assertEqual(target.status, "processing")

    def test_no_redis_url_leaves_the_row_alone(self):
        target = row("processing")
        with mock.patch.dict("os.environ", {"REDIS_URL": ""}):
            _reconcile_dead_effect(self.db, target)
        self.assertEqual(target.status, "processing")


if __name__ == "__main__":
    unittest.main()
