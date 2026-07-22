from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def should_supervise_local_worker(redis_url: str) -> bool:
    configured = os.getenv("AUTO_START_RQ_WORKER", "").strip().lower()
    if configured in _TRUE_VALUES:
        return True
    if configured in _FALSE_VALUES:
        return False
    return urlparse(redis_url).hostname in _LOCAL_HOSTS


def _rq_executable() -> str | None:
    alongside_python = Path(sys.executable).with_name("rq")
    if alongside_python.is_file():
        return str(alongside_python)
    return shutil.which("rq")


def start_local_worker(redis_url: str) -> subprocess.Popen[bytes] | None:
    if not should_supervise_local_worker(redis_url):
        return None

    executable = _rq_executable()
    if not executable:
        raise RuntimeError("RQ executable not found")

    env = os.environ.copy()
    env["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"
    env["PYTHONUNBUFFERED"] = "1"
    project_root = Path(__file__).resolve().parents[2]
    return subprocess.Popen(
        [executable, "worker", "--verbose", "-u", redis_url, "default"],
        cwd=project_root,
        env=env,
        start_new_session=True,
    )


def stop_local_worker(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
