from __future__ import annotations

import os
import unittest
from unittest import mock

from app.services.local_worker_manager import (
    should_supervise_local_worker,
    start_local_worker,
)


class LocalWorkerConfigurationTests(unittest.TestCase):
    def test_local_redis_is_supervised_by_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AUTO_START_RQ_WORKER", None)
            self.assertTrue(should_supervise_local_worker("redis://127.0.0.1:6379/0"))
            self.assertTrue(should_supervise_local_worker("redis://localhost:6379/0"))

    def test_remote_redis_is_not_supervised_by_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AUTO_START_RQ_WORKER", None)
            self.assertFalse(
                should_supervise_local_worker("rediss://cache.example.com:6379/0")
            )

    def test_explicit_setting_overrides_redis_host(self) -> None:
        with mock.patch.dict(os.environ, {"AUTO_START_RQ_WORKER": "false"}):
            self.assertFalse(should_supervise_local_worker("redis://localhost:6379/0"))
        with mock.patch.dict(os.environ, {"AUTO_START_RQ_WORKER": "true"}):
            self.assertTrue(
                should_supervise_local_worker("rediss://cache.example.com:6379/0")
            )

    @mock.patch("app.services.local_worker_manager.subprocess.Popen")
    @mock.patch("app.services.local_worker_manager._rq_executable", return_value="/venv/bin/rq")
    def test_starts_default_queue_worker(
        self,
        _executable: mock.Mock,
        popen: mock.Mock,
    ) -> None:
        worker = mock.Mock()
        popen.return_value = worker
        with mock.patch.dict(os.environ, {"AUTO_START_RQ_WORKER": "true"}):
            result = start_local_worker("redis://localhost:6379/0")

        self.assertIs(result, worker)
        command = popen.call_args.args[0]
        self.assertEqual(
            command,
            [
                "/venv/bin/rq",
                "worker",
                "--verbose",
                "-u",
                "redis://localhost:6379/0",
                "default",
            ],
        )


if __name__ == "__main__":
    unittest.main()
