"""Single-frame segmentation preview.

This is the interactive half of background removal: the user clicks, sees the
mask immediately, adjusts, and only then runs the full clip. Without it, every
refinement costs a queued job and a re-watch, which is the difference between a
tool and a lottery.

Deliberately synchronous and deliberately not a queued job:

  * It has to be fast enough to feel like a response to a click. One frame on
    MPS is ~0.2s once weights are warm, so a round trip is fine; a queue round
    trip is not.
  * Running in the API process is also what keeps it *safe*. The RQ worker forks
    per job, and torch after fork dies inside Metal rather than raising — see
    `sam2_backend._assert_fork_safe`. A single-process uvicorn never forks, so
    the model stays in a process that can actually use the GPU.

Consequence worth stating plainly: this occupies a request thread while it runs.
That is acceptable for one frame at interactive rates and is not a path to batch
work through — the whole-clip run stays on the queue.
"""

from __future__ import annotations

import subprocess
from typing import Any

from .base import SegmentationError


def extract_frame(source: str, at_seconds: float) -> Any:
    """Decodes a single frame at `at_seconds` as an RGB numpy array.

    `-ss` before `-i` so ffmpeg seeks rather than decoding up to the timestamp;
    on a long source that is the difference between instant and several seconds.
    Accuracy is fine for this purpose — the preview only has to show the frame
    the user is looking at, and the editor's own seek is keyframe-bound too.
    """
    import numpy as np

    command = [
        "ffmpeg",
        "-nostdin",
        *(["-ss", f"{max(0.0, at_seconds):.3f}"] if at_seconds > 0 else []),
        "-i", source,
        "-frames:v", "1",
        # Raw RGB straight to stdout: no temp file, no PNG encode/decode round
        # trip on a path that is supposed to feel instant.
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "-",
    ]
    process = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if process.returncode != 0 or not process.stdout:
        tail = process.stderr.decode("utf-8", "replace").strip().splitlines()
        raise SegmentationError(
            "Could not read a frame at that position: " + (tail[-1] if tail else "unknown error")
        )

    width, height = _probe_size(source)
    expected = width * height * 3
    if len(process.stdout) < expected:
        raise SegmentationError("The decoded frame was incomplete.")

    # `.copy()` because frombuffer over immutable bytes yields a read-only array,
    # and torch warns that tensors sharing non-writable memory have undefined
    # write behaviour. Cheap at one frame, and not worth an undefined-behaviour
    # warning in the log on every click.
    return (
        np.frombuffer(process.stdout[:expected], dtype=np.uint8)
        .reshape(height, width, 3)
        .copy()
    )


def _probe_size(source: str) -> tuple[int, int]:
    """Frame size from ffprobe.

    Needed because rawvideo on stdout carries no dimensions — reshaping by the
    wrong width silently shears the image rather than failing, which would then
    look like the model mis-segmenting.
    """
    process = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0:s=x",
            source,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    raw = process.stdout.decode("utf-8", "replace").strip().splitlines()
    if process.returncode != 0 or not raw:
        raise SegmentationError("Could not read the video's dimensions.")
    try:
        width, height = (int(part) for part in raw[0].split("x")[:2])
    except ValueError as exc:
        raise SegmentationError("Could not read the video's dimensions.") from exc
    if not width or not height:
        raise SegmentationError("Could not read the video's dimensions.")
    return width, height


def preview_mask_png(
    source: str,
    at_seconds: float,
    points: list[tuple[float, float]],
    labels: list[int],
    *,
    quality: str = "faster",
    settings: dict[str, Any] | None = None,
) -> tuple[bytes, int, int]:
    """Returns `(png_bytes, width, height)` for a one-frame mask preview.

    The mask is an 8-bit greyscale PNG, not an RGBA cutout. That lets the client
    use it as a CSS mask and tint it with a theme token, so the overlay colour
    follows light/dark mode instead of being baked in by the server. It is also
    a fraction of the bytes of a colour image.
    """
    import cv2  # type: ignore

    from . import sam2_backend

    if not sam2_backend.is_installed():
        raise SegmentationError(
            "Click-to-select needs SAM 2. Use Python 3.11-3.13, run "
            "`./scripts/setup_ml_env.sh`, then restart the API and worker."
        )

    frame = extract_frame(source, at_seconds)
    mask = sam2_backend.segment_at_points(frame, points, labels, quality=quality)

    return _encode_preview(frame, mask, settings)


def preview_auto_mask_png(
    source: str,
    at_seconds: float,
    *,
    quality: str = "faster",
    settings: dict[str, Any] | None = None,
) -> tuple[bytes, int, int]:
    """Automatic one-frame preview using the same model as the queued render."""
    import cv2  # type: ignore

    from .auto_backend import segment_auto

    frame_rgb = extract_frame(source, at_seconds)
    frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
    mask = segment_auto(frame_bgr, quality=quality)
    return _encode_preview(frame_rgb, mask, settings)


def _encode_preview(
    frame_rgb: Any,
    mask: Any,
    settings: dict[str, Any] | None,
) -> tuple[bytes, int, int]:
    """Applies shared refinement/keying and encodes a greyscale PNG."""
    import cv2  # type: ignore

    # The same refinement the export applies, from the same settings, through
    # the same function. This is the WYSIWYG boundary for mask tuning.
    from .matte_ops import matte_settings_from_attributes, refine_matte
    from .chroma_matte import chroma_keep_matte, combine_keep_mattes

    mask = refine_matte(mask, matte_settings_from_attributes(settings))
    if (settings or {}).get("chromaKey"):
        frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
        mask = combine_keep_mattes(mask, chroma_keep_matte(frame_bgr, settings or {}))

    ok, buffer = cv2.imencode(".png", mask)
    if not ok:
        raise SegmentationError("Could not encode the mask preview.")

    height, width = mask.shape[:2]
    return buffer.tobytes(), width, height
