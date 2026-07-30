"""Runs background removal in a separate interpreter instead of the caller's.

Why this exists, concretely. The RQ worker forks a child per job, and on macOS a
forked child cannot safely use the things this work needs. Three separate crashes
came out of one afternoon, all `parentProc: Python`, all signals rather than
exceptions, so the worker died mid-job and logged nothing:

  * SIGSEGV in `_scproxy.get_proxies` -> `SCDynamicStoreCopyProxiesWithOptions`
    -> CFPreferences. SystemConfiguration is not fork-safe, and *any* HTTP client
    reaches it — including `from_pretrained` checking HuggingFace for a
    checkpoint it already has cached. Reproducible in three lines: look up
    proxies in a parent, fork, look them up again.
  * SIGSEGV in `at::mps::MPSAllocator::allocate` -> `IOGPUDeviceGetAllocatedSize`.
    The inherited GPU device handle is not valid in the child.
  * SIGABRT inside Metal's shader compiler.

An earlier attempt guarded only the narrow case where the *parent* had already
run inference. That was measured, but measured wrongly: the "clean parent forking
children works" result held only because the checkpoint was already cached, so no
network call happened. The broader conclusion is the correct one — torch and
HuggingFace must not run in a forked child at all.

A fresh interpreter inherits none of it: no CoreFoundation state, no Metal
context, no GPU handles. All three crash modes go away together rather than being
patched one at a time.

Cost is one interpreter start and one model load per job — seconds, against a job
that already takes minutes. The interactive preview deliberately does *not* come
through here: it runs in-process under uvicorn, which never forks, because paying
seconds per click would defeat the point of a preview.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .base import SegmentationError, SegmentationResult

#: Set to "0" to run in-process. Useful when debugging, since a subprocess hides
#: the traceback; unsafe in a forking worker, which is why it defaults on.
ISOLATE_ENV = "SEGMENTATION_ISOLATE"

#: Hard ceiling on the child. The clip-length limit already bounds the real work;
#: this only stops a wedged process from holding the job forever.
_TIMEOUT_SEC = float(os.environ.get("SEGMENTATION_ISOLATE_TIMEOUT_SEC", "3600") or "3600")


def isolation_enabled() -> bool:
    return (os.environ.get(ISOLATE_ENV, "1") or "1").strip().lower() not in {"0", "false", "no"}


def _child_command() -> list[str]:
    return [sys.executable, "-m", "app.services.segmentation.child"]


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    # The child must import `app`, and it may be launched from a worker whose cwd
    # is elsewhere. Anchor on this file rather than the cwd: four parents up from
    # app/services/segmentation/isolated.py is the directory holding `app`.
    root = str(Path(__file__).resolve().parents[3])
    existing = env.get("PYTHONPATH", "")
    if root not in existing.split(os.pathsep):
        env["PYTHONPATH"] = os.pathsep.join([root, existing]) if existing else root
    # The child is the only thing doing inference; unbuffered stdout is what makes
    # incremental progress possible.
    env["PYTHONUNBUFFERED"] = "1"
    return env


def remove_background_isolated(
    source: str,
    clip_target: dict[str, Any],
    settings: dict[str, Any],
    output_dir: Path,
    *,
    progress: Any = None,
) -> SegmentationResult:
    """Runs the removal in a subprocess, forwarding progress as it arrives."""
    payload = json.dumps(
        {
            "source": source,
            "clip_target": clip_target,
            "settings": settings,
            # The child writes into the caller's directory, so the finished video
            # never has to travel through a pipe.
            "output_dir": str(output_dir),
        }
    )

    process = subprocess.Popen(
        _child_command(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_child_env(),
        cwd=str(Path(__file__).resolve().parents[3]),
        text=True,
    )
    assert process.stdin is not None and process.stdout is not None

    try:
        process.stdin.write(payload)
        process.stdin.close()
    except (BrokenPipeError, OSError) as exc:
        process.kill()
        raise SegmentationError(f"Could not start background removal: {exc}") from exc

    outcome: dict[str, Any] | None = None
    try:
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                # A library printing to stdout is noise, not a protocol error.
                continue
            if "progress" in message:
                if progress:
                    progress(message["progress"])
                continue
            outcome = message
            break
        process.wait(timeout=_TIMEOUT_SEC)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        raise SegmentationError(
            f"Background removal timed out after {int(_TIMEOUT_SEC)}s."
        ) from exc

    stderr = (process.stderr.read() if process.stderr else "") or ""

    if outcome is None:
        code = process.returncode
        # A negative return code is the signal that killed it. This is the case
        # that used to take the whole worker down, so name it rather than raising
        # something generic.
        if code is not None and code < 0:
            raise SegmentationError(
                f"Background removal crashed (signal {-code}). Try "
                f"SEGMENTATION_DEVICE=cpu, or SEGMENTATION_PROVIDER=http to run the "
                f"model outside this server."
            )
        detail = stderr.strip().splitlines()[-1] if stderr.strip() else "no output"
        raise SegmentationError(
            f"Background removal exited without a result (code {code}): {detail}"
        )

    if "error" in outcome:
        raise SegmentationError(str(outcome["error"]))

    if outcome.get("url"):
        return SegmentationResult(url=str(outcome["url"]))

    path = outcome.get("done")
    if not path or not Path(str(path)).exists():
        raise SegmentationError("Background removal reported success but produced no file.")
    return SegmentationResult(path=Path(str(path)))
