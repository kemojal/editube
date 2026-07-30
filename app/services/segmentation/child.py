"""Isolated background-removal worker, run as `python -m app.services.segmentation.child`.

Invoked as a subprocess by `isolated.py`. Reads one JSON payload on stdin and
writes line-delimited JSON progress to stdout. See `isolated.py` for why the work
has to happen in a separate interpreter at all.

A module entrypoint rather than `multiprocessing`: spawn re-imports the parent's
`__main__` before running anything, which for an RQ worker is the `rq` console
script. Relying on that script's `if __name__ == "__main__"` guard to stop a
second worker from starting is not a bet worth taking, and it broke immediately
under a `python -` parent. `-m` has no main fixup, needs no pickling, and can be
run by hand with the same payload when something needs debugging.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path


#: How often the orphan watchdog checks. Cheap enough to be frequent, and the
#: worst case is this much GPU time wasted after a cancel.
_ORPHAN_POLL_SEC = 2.0


def _watch_for_orphaning() -> None:
    """Exits if the parent process goes away.

    Necessary because cancelling a running job means RQ sends SIGKILL to the
    work-horse, and this process is its child. A SIGKILLed parent cannot clean up,
    so without this the segmentation would carry on to completion — holding the
    GPU and writing a cutout nobody asked for any more, well after the user
    cancelled and the UI said so.

    Polling getppid rather than a signal because SIGKILL leaves no opportunity to
    notify anyone: the only observable is that the parent is gone. On macOS and
    Linux an orphan is reparented (to launchd or init), so the pid changing is the
    signal.
    """
    original = os.getppid()

    def watch() -> None:
        while True:
            time.sleep(_ORPHAN_POLL_SEC)
            if os.getppid() != original:
                # _exit, not sys.exit: there is nobody left to report to, and
                # interpreter shutdown could block on the torch teardown.
                os._exit(143)

    threading.Thread(target=watch, daemon=True, name="orphan-watchdog").start()


def _emit(message: dict) -> None:
    # One JSON object per line, flushed: the parent reads this incrementally to
    # drive the progress bar, so buffering would hold every update until exit.
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main() -> int:
    _watch_for_orphaning()

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        _emit({"error": f"Malformed payload: {exc}"})
        return 2

    try:
        from app.services.segmentation.base import SegmentationError
        from app.services.segmentation.local import LocalSegmentationProvider

        try:
            result = LocalSegmentationProvider().remove_background_inprocess(
                payload["source"],
                payload.get("clip_target") or {},
                payload.get("settings") or {},
                Path(payload["output_dir"]),
                progress=lambda value: _emit({"progress": value}),
            )
        except SegmentationError as exc:
            # Expected, user-facing failures: the message is already written for
            # the user, so pass it through rather than wrapping it in a traceback.
            _emit({"error": str(exc)})
            return 1

        _emit({"done": str(result.path) if result.path else None, "url": result.url})
        return 0
    except BaseException as exc:  # noqa: BLE001 - the reason must reach the parent
        _emit({"error": f"{type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
