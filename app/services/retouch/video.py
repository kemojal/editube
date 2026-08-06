"""Whole-clip retouch renderer using the same frame function as preview."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import cv2  # type: ignore

from .beauty import BeautyState, beautify_frame


MAX_RETOUCH_SECONDS = float(os.environ.get("RETOUCH_LOCAL_MAX_SECONDS", "180") or "180")


def render_retouch_video(
    source: str,
    clip_target: dict[str, Any],
    settings: dict[str, Any],
    output: Path,
    *,
    progress: Any = None,
    cancel: Any = None,
) -> Path:
    capture = cv2.VideoCapture(source)
    if not capture.isOpened():
        raise RuntimeError("Could not read the clip for retouching.")
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 25.0)
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    source_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    start = max(0.0, float(clip_target.get("start") or 0.0))
    end = float(clip_target.get("end") or 0.0)
    seconds = end - start if end > start else source_frames / max(fps, 1.0)
    if not width or not height or seconds <= 0:
        capture.release()
        raise RuntimeError("The selected clip has no readable video frames.")
    if seconds > MAX_RETOUCH_SECONDS:
        capture.release()
        raise RuntimeError(
            f"This clip is {int(seconds)}s, over the {int(MAX_RETOUCH_SECONDS)}s local retouch limit. "
            "Trim the clip or configure a GPU beauty provider."
        )
    if start > 0:
        capture.set(cv2.CAP_PROP_POS_MSEC, start * 1000.0)
    total = max(1, int(round(seconds * fps)))
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg", "-y",
        "-f", "rawvideo",
        "-pix_fmt", "bgr24",
        "-s", f"{width}x{height}",
        "-r", f"{fps:.6f}",
        "-i", "-",
        "-an",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output),
    ]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    assert process.stdin is not None
    state = BeautyState()
    rendered = 0
    last_progress = -1
    try:
        while rendered < total:
            if cancel and rendered % max(1, int(fps / 2)) == 0 and cancel():
                raise RuntimeError("Retouching was cancelled.")
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            process.stdin.write(beautify_frame(frame, settings, state).tobytes())
            rendered += 1
            if progress:
                next_progress = min(92, 10 + int(rendered / total * 82))
                if next_progress > last_progress:
                    progress(next_progress)
                    last_progress = next_progress
    except BrokenPipeError:
        pass
    finally:
        capture.release()
        try:
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        process.stdin = None
        stderr = process.stderr.read() if process.stderr is not None else b""
        if process.stderr is not None:
            process.stderr.close()
        process.wait()
    if rendered == 0:
        raise RuntimeError("No frames were available in the selected retouch range.")
    if process.returncode != 0:
        detail = (stderr.decode("utf-8", "replace").strip().splitlines() or ["unknown encoder error"])[-1]
        raise RuntimeError(f"Retouching failed while writing the clip: {detail}")
    return output
