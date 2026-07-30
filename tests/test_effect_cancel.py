"""Cancelling an effect.

The original cancel only called `job.cancel()`, which removes a job from the
queue and does nothing at all to one already running. So cancelling a started job
marked the row failed while the work-horse carried on burning GPU — and when it
finished it wrote `completed` over the cancel, so the effect applied anyway.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from app.jobs.rough_cut_effect import _was_canceled


class WasCanceledTests(unittest.TestCase):
    """The guard that stops a finishing job from overwriting a cancel."""

    def _db(self):
        return mock.MagicMock()

    def test_a_normal_row_is_not_canceled(self):
        row = SimpleNamespace(status="processing", result_data={"progress": 40})
        self.assertFalse(_was_canceled(self._db(), row))

    def test_the_canceled_flag_is_honoured(self):
        # Written by the API process, which is why the job has to re-read rather
        # than trust the copy it loaded at the start.
        row = SimpleNamespace(status="processing", result_data={"canceled": True})
        self.assertTrue(_was_canceled(self._db(), row))

    def test_a_canceled_status_is_honoured(self):
        row = SimpleNamespace(status="canceled", result_data={})
        self.assertTrue(_was_canceled(self._db(), row))

    def test_the_row_is_expired_so_a_stale_copy_cannot_win(self):
        # The whole point: without expire() the session hands back the version
        # loaded before the cancel was written.
        db = self._db()
        row = SimpleNamespace(status="processing", result_data={})
        _was_canceled(db, row)
        db.expire.assert_called_once_with(row)

    def test_missing_result_data_does_not_raise(self):
        row = SimpleNamespace(status="processing", result_data=None)
        self.assertFalse(_was_canceled(self._db(), row))


class CompletionGuardTests(unittest.TestCase):
    def test_complete_is_skipped_for_a_canceled_row(self):
        from app.jobs import rough_cut_effect as job_module

        row = SimpleNamespace(status="canceled", result_data={"canceled": True})
        with mock.patch.object(job_module, "_update_row") as update, mock.patch.object(
            job_module, "_attach_to_draft"
        ) as attach:
            job_module._complete(mock.MagicMock(), row, "clip-a", "remove_bg", "http://x/y.webm")
        update.assert_not_called()
        attach.assert_not_called()

    def test_complete_runs_normally_otherwise(self):
        from app.jobs import rough_cut_effect as job_module

        row = SimpleNamespace(id=1, video_id=43, status="processing", result_data={})
        with mock.patch.object(job_module, "_update_row") as update, mock.patch.object(
            job_module, "_attach_to_draft"
        ) as attach:
            job_module._complete(mock.MagicMock(), row, "clip-a", "remove_bg", "http://x/y.webm")
        update.assert_called_once()
        attach.assert_called_once()

    def test_failure_is_not_reported_for_a_canceled_row(self):
        # A cancelled job dies by signal or raises on a closed pipe. Surfacing
        # that as an error shows the user a problem they created on purpose.
        from app.jobs import rough_cut_effect as job_module

        row = SimpleNamespace(
            status="canceled", result_data={"canceled": True}, error_message=None
        )
        db = mock.MagicMock()
        with mock.patch.object(job_module, "_attach_to_draft") as attach:
            job_module._fail(db, row, "Work-horse terminated unexpectedly")
        self.assertEqual(row.status, "canceled")
        self.assertIsNone(row.error_message)
        attach.assert_not_called()


if __name__ == "__main__":
    unittest.main()
