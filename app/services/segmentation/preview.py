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
import threading
from collections import OrderedDict
from typing import Any

from .base import SegmentationError

#: Guards every cache in this module. Held only around dict operations, never
#: across a decode or an inference call — those are slow and already serialised
#: where they need to be.
_CACHE_LOCK = threading.Lock()

#: Decoded frames, newest last, keyed by `_frame_key`.
#:
#: The click loop asks for the *same* frame over and over: click, refine, adjust
#: feather, click again. Without this every one of those paid an ffprobe plus an
#: ffmpeg decode, and when the media lives on an object store that decode is a
#: network fetch — seconds of it, on a path whose whole job is to answer a click.
#: Three entries is enough for "the frame being refined" plus a scrub away and
#: back, and costs ~6MB per 1080p entry.
_FRAME_CACHE: "OrderedDict[str, Any]" = OrderedDict()
_FRAME_CACHE_LIMIT = 3

#: Frame sizes per source. Constant for the life of a file, so re-probing it on
#: every click was pure subprocess overhead.
_SIZE_CACHE: "OrderedDict[str, tuple[int, int]]" = OrderedDict()
_SIZE_CACHE_LIMIT = 64

#: Raw (pre-refinement) mattes keyed by frame + prompts + quality.
#:
#: Feather, grow, invert and opacity are post-processing on a matte the model
#: already produced, but they are part of the request identity because the
#: server applies them. Without this cache, dragging the Feather slider re-ran
#: segmentation for a result that could not change — the single most expensive
#: no-op in the editor.
_MASK_CACHE: "OrderedDict[str, Any]" = OrderedDict()
_MASK_CACHE_LIMIT = 8


def _remember(cache: "OrderedDict[str, Any]", key: str, value: Any, limit: int) -> Any:
    with _CACHE_LOCK:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > limit:
            cache.popitem(last=False)
    return value


def _recall(cache: "OrderedDict[str, Any]", key: str) -> Any:
    with _CACHE_LOCK:
        value = cache.get(key)
        if value is not None:
            cache.move_to_end(key)
        return value


def frame_key(source: str, at_seconds: float) -> str:
    """Identity of one decoded frame.

    Quantised to the millisecond because that is the resolution the client can
    ask for and far finer than any real frame duration — two requests for the
    same on-screen frame must land on the same key or every cache here misses.
    """
    return f"{source}@{int(round(max(0.0, at_seconds) * 1000))}"


def clear_preview_caches() -> None:
    """Drops decoded frames, sizes and mattes. For tests; not on the request path."""
    with _CACHE_LOCK:
        _FRAME_CACHE.clear()
        _SIZE_CACHE.clear()
        _MASK_CACHE.clear()


def extract_frame(source: str, at_seconds: float) -> Any:
    """Decodes a single frame at `at_seconds` as an RGB numpy array.

    `-ss` before `-i` so ffmpeg seeks rather than decoding up to the timestamp;
    on a long source that is the difference between instant and several seconds.
    Accuracy is fine for this purpose — the preview only has to show the frame
    the user is looking at, and the editor's own seek is keyframe-bound too.

    Cached by `_FRAME_CACHE`. Callers must treat the array as read-only: it is
    shared between the requests that hit the same frame.
    """
    import numpy as np

    key = frame_key(source, at_seconds)
    cached = _recall(_FRAME_CACHE, key)
    if cached is not None:
        return cached

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
    frame = (
        np.frombuffer(process.stdout[:expected], dtype=np.uint8)
        .reshape(height, width, 3)
        .copy()
    )
    return _remember(_FRAME_CACHE, key, frame, _FRAME_CACHE_LIMIT)


def _probe_size(source: str) -> tuple[int, int]:
    """Frame size from ffprobe.

    Needed because rawvideo on stdout carries no dimensions — reshaping by the
    wrong width silently shears the image rather than failing, which would then
    look like the model mis-segmenting.

    Cached per source, and only on success — a failure here is "this is not a
    readable video", which must be raised again rather than answered from a
    cache the caller cannot see.
    """
    cached = _recall(_SIZE_CACHE, source)
    if cached is not None:
        return cached

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
    return _remember(_SIZE_CACHE, source, (width, height), _SIZE_CACHE_LIMIT)


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

    key = frame_key(source, at_seconds)
    frame = extract_frame(source, at_seconds)
    signature = f"{key}|points|{quality}|{list(zip(points, labels))!r}"
    mask = _recall(_MASK_CACHE, signature)
    if mask is None:
        mask = sam2_backend.segment_at_points(
            frame, points, labels, quality=quality, frame_key=key
        )
        _remember(_MASK_CACHE, signature, mask, _MASK_CACHE_LIMIT)

    return _encode_preview(frame, mask, settings)


def preview_selection_mask_png(
    source: str,
    at_seconds: float,
    prompts: Any,
    *,
    quality: str = "faster",
    settings: dict[str, Any] | None = None,
) -> tuple[bytes, int, int]:
    """Preview a composed selection using the editor's additive semantics."""
    from . import sam2_backend

    if not sam2_backend.is_installed():
        raise SegmentationError(
            "Click-to-select needs SAM 2. Use Python 3.11-3.13, run "
            "`./scripts/setup_ml_env.sh`, then restart the API and worker."
        )

    key = frame_key(source, at_seconds)
    frame = extract_frame(source, at_seconds)
    # Only what the model sees goes into the signature. Refinement is applied
    # below, on the cached matte, so moving Feather never re-segments.
    signature = (
        f"{key}|groups|{quality}|"
        f"{prompts.positive_groups!r}|{prompts.negative_points!r}"
    )
    mask = _recall(_MASK_CACHE, signature)
    if mask is None:
        mask = sam2_backend.segment_prompt_groups(
            frame,
            prompts.positive_groups,
            prompts.negative_points,
            quality=quality,
            frame_key=key,
        )
        _remember(_MASK_CACHE, signature, mask, _MASK_CACHE_LIMIT)
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

    key = frame_key(source, at_seconds)
    frame_rgb = extract_frame(source, at_seconds)
    signature = f"{key}|auto|{quality}"
    mask = _recall(_MASK_CACHE, signature)
    if mask is None:
        frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
        mask = segment_auto(frame_bgr, quality=quality)
        _remember(_MASK_CACHE, signature, mask, _MASK_CACHE_LIMIT)
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

    # `mask` may be a cached matte shared with the next request, so the refined
    # result must be a new array. `refine_matte` returns one for every setting it
    # actually applies; the copy covers the no-op path, where it hands the input
    # straight back and an in-place chroma combine below would poison the cache.
    mask = refine_matte(mask, matte_settings_from_attributes(settings)).copy()
    if (settings or {}).get("chromaKey"):
        frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
        mask = combine_keep_mattes(mask, chroma_keep_matte(frame_bgr, settings or {}))

    ok, buffer = cv2.imencode(".png", mask)
    if not ok:
        raise SegmentationError("Could not encode the mask preview.")

    height, width = mask.shape[:2]
    return buffer.tobytes(), width, height
