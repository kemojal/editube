"""Isolation of background removal into a separate interpreter.

The reason this exists is a crash, not a design preference: an RQ worker forks
per job, and a forked child on macOS dies inside SystemConfiguration, the MPS
allocator, or the Metal compiler — by signal, so the worker takes the job down
with it and logs nothing. `isolated.py` documents the three signatures.

These tests cover the parts that can be asserted without a GPU: the switch, the
child protocol, and that a failure comes back as a message rather than silence.
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from app.services.segmentation import isolated
from app.services.segmentation.base import SegmentationError

ROOT = str(Path(__file__).resolve().parents[1])


class IsolationSwitchTests(unittest.TestCase):
    def test_isolation_is_on_by_default(self):
        # The default has to be the safe one: in-process means crashing the
        # worker on macOS, and a crash is not a failure mode anyone debugs twice.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SEGMENTATION_ISOLATE", None)
            self.assertTrue(isolated.isolation_enabled())

    def test_it_can_be_switched_off_for_debugging(self):
        for value in ("0", "false", "no", "FALSE"):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {"SEGMENTATION_ISOLATE": value}):
                    self.assertFalse(isolated.isolation_enabled())

    def test_anything_else_leaves_isolation_on(self):
        for value in ("1", "true", "yes", "", "garbage"):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {"SEGMENTATION_ISOLATE": value}):
                    self.assertTrue(isolated.isolation_enabled())


class ChildEnvironmentTests(unittest.TestCase):
    def test_pythonpath_anchors_on_the_repo_not_the_cwd(self):
        # The worker's cwd is not guaranteed to be the app root, and a child that
        # cannot import `app` would fail in a way that looks like a model problem.
        with mock.patch.dict(os.environ, {"PYTHONPATH": ""}, clear=False):
            env = isolated._child_env()
        self.assertIn(ROOT, env["PYTHONPATH"].split(os.pathsep))

    def test_an_existing_pythonpath_is_preserved(self):
        with mock.patch.dict(os.environ, {"PYTHONPATH": "/somewhere/else"}):
            env = isolated._child_env()
        parts = env["PYTHONPATH"].split(os.pathsep)
        self.assertIn(ROOT, parts)
        self.assertIn("/somewhere/else", parts)

    def test_the_repo_root_is_not_added_twice(self):
        with mock.patch.dict(os.environ, {"PYTHONPATH": ROOT}):
            env = isolated._child_env()
        self.assertEqual(env["PYTHONPATH"].split(os.pathsep).count(ROOT), 1)

    def test_the_child_runs_unbuffered_so_progress_arrives_incrementally(self):
        self.assertEqual(isolated._child_env()["PYTHONUNBUFFERED"], "1")

    def test_the_child_is_invoked_as_a_module_not_a_script(self):
        # `-m` rather than multiprocessing spawn: spawn re-imports the parent's
        # __main__ first, which for an RQ worker is the `rq` console script.
        command = isolated._child_command()
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[1], "-m")
        self.assertTrue(command[2].endswith("segmentation.child"))


class ChildProtocolTests(unittest.TestCase):
    """Drives the child process directly, which needs no model."""

    def run_child(self, payload):
        process = subprocess.run(
            isolated._child_command(),
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=isolated._child_env(),
            cwd=ROOT,
            timeout=120,
        )
        messages = []
        for line in process.stdout.splitlines():
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return process, messages

    def test_a_malformed_payload_is_reported_not_crashed_on(self):
        process = subprocess.run(
            isolated._child_command(),
            input="{not json",
            capture_output=True,
            text=True,
            env=isolated._child_env(),
            cwd=ROOT,
            timeout=120,
        )
        self.assertEqual(process.returncode, 2)
        self.assertIn("error", json.loads(process.stdout.splitlines()[-1]))

    def test_an_unreadable_source_comes_back_as_an_error_message(self):
        process, messages = self.run_child(
            {
                "source": "/nonexistent/clip.mp4",
                "clip_target": {"start": 0, "end": 1},
                "settings": {"autoRemoval": True},
                "output_dir": "/tmp",
            }
        )
        self.assertNotEqual(process.returncode, 0)
        self.assertTrue(messages, "the child must always say why it failed")
        self.assertIn("error", messages[-1])

    def test_the_child_never_exits_silently(self):
        # Silence is the failure mode this whole module exists to remove.
        _, messages = self.run_child({"source": "", "output_dir": "/tmp"})
        self.assertTrue(messages)
        self.assertIn("error", messages[-1])


class OutcomeHandlingTests(unittest.TestCase):
    """The parent's interpretation of how the child ended."""

    def _fake_process(self, *, stdout_lines, returncode, stderr=""):
        class FakeStream:
            def __init__(self, lines):
                self._lines = list(lines)

            def __iter__(self):
                return iter(self._lines)

            def read(self):
                return stderr

            def write(self, _value):
                return None

            def close(self):
                return None

        process = mock.MagicMock()
        process.stdin = FakeStream([])
        process.stdout = FakeStream(stdout_lines)
        process.stderr = FakeStream([])
        process.returncode = returncode
        process.wait.return_value = returncode
        return process

    def test_a_signal_death_is_named_as_a_crash(self):
        # Negative exit code == killed by a signal. The old behaviour was for this
        # to take the worker with it, so the message has to be actionable.
        process = self._fake_process(stdout_lines=[], returncode=-11)
        with mock.patch.object(isolated.subprocess, "Popen", return_value=process):
            with self.assertRaises(SegmentationError) as caught:
                isolated.remove_background_isolated("src.mp4", {}, {}, Path("/tmp"))
        message = str(caught.exception)
        self.assertIn("signal 11", message)
        self.assertIn("SEGMENTATION_DEVICE=cpu", message)

    def test_a_reported_error_is_passed_through_verbatim(self):
        process = self._fake_process(
            stdout_lines=[json.dumps({"error": "This clip is 300s, over the 120s limit."})],
            returncode=1,
        )
        with mock.patch.object(isolated.subprocess, "Popen", return_value=process):
            with self.assertRaises(SegmentationError) as caught:
                isolated.remove_background_isolated("src.mp4", {}, {}, Path("/tmp"))
        self.assertIn("over the 120s limit", str(caught.exception))

    def test_progress_is_forwarded_as_it_arrives(self):
        seen: list[int] = []
        process = self._fake_process(
            stdout_lines=[
                json.dumps({"progress": 10}),
                json.dumps({"progress": 55}),
                json.dumps({"error": "stop here"}),
            ],
            returncode=1,
        )
        with mock.patch.object(isolated.subprocess, "Popen", return_value=process):
            with self.assertRaises(SegmentationError):
                isolated.remove_background_isolated(
                    "src.mp4", {}, {}, Path("/tmp"), progress=seen.append
                )
        self.assertEqual(seen, [10, 55])

    def test_stray_stdout_from_a_library_is_ignored(self):
        # Model libraries print banners. That is noise, not a protocol violation,
        # and must not be mistaken for the outcome.
        process = self._fake_process(
            stdout_lines=[
                "Loading checkpoint shards: 100%|##########|",
                json.dumps({"progress": 42}),
                json.dumps({"error": "done being noisy"}),
            ],
            returncode=1,
        )
        seen: list[int] = []
        with mock.patch.object(isolated.subprocess, "Popen", return_value=process):
            with self.assertRaises(SegmentationError) as caught:
                isolated.remove_background_isolated(
                    "src.mp4", {}, {}, Path("/tmp"), progress=seen.append
                )
        self.assertEqual(seen, [42])
        self.assertIn("done being noisy", str(caught.exception))

    def test_success_claiming_a_missing_file_is_not_believed(self):
        process = self._fake_process(
            stdout_lines=[json.dumps({"done": "/nonexistent/cutout.webm", "url": None})],
            returncode=0,
        )
        with mock.patch.object(isolated.subprocess, "Popen", return_value=process):
            with self.assertRaises(SegmentationError):
                isolated.remove_background_isolated("src.mp4", {}, {}, Path("/tmp"))

    def test_a_url_result_is_returned_without_touching_the_filesystem(self):
        process = self._fake_process(
            stdout_lines=[json.dumps({"done": None, "url": "https://cdn/example.webm"})],
            returncode=0,
        )
        with mock.patch.object(isolated.subprocess, "Popen", return_value=process):
            result = isolated.remove_background_isolated("src.mp4", {}, {}, Path("/tmp"))
        self.assertEqual(result.url, "https://cdn/example.webm")


if __name__ == "__main__":
    unittest.main()
