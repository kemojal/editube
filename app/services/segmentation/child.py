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
import sys
from pathlib import Path


def _emit(message: dict) -> None:
    # One JSON object per line, flushed: the parent reads this incrementally to
    # drive the progress bar, so buffering would hold every update until exit.
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main() -> int:
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
