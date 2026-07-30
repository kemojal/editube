"""SAM 2 point-prompt segmentation.

Kept separate from `local.py` so importing the local provider never pulls torch
in. `rembg`-only deployments stay light, and a missing SAM 2 degrades to "point
prompts unavailable" rather than breaking auto matting as well.

Device selection matters more than it looks on a developer machine: Apple
silicon runs this on MPS, which is roughly an order of magnitude faster than the
CPU fallback and makes the click-refine-preview loop usable rather than
theoretical.
"""

from __future__ import annotations

import functools
import importlib.util
import os
from typing import Any

from .base import SegmentationError

#: Checkpoint per quality tier. Tiny is genuinely interactive; large is for a
#: final pass where edge quality matters more than latency.
MODEL_IDS = {
    "faster": "facebook/sam2.1-hiera-tiny",
    "better": "facebook/sam2.1-hiera-large",
}


#: PID that first put torch to work here. See `_assert_fork_safe`.
_TORCH_OWNER_PID: int | None = None


def is_installed() -> bool:
    # find_spec rather than import: the capability handshake calls this on every
    # job, and importing torch to answer "no" would cost seconds per job — and,
    # worse, would poison the process for forking (see `_assert_fork_safe`).
    return all(importlib.util.find_spec(name) is not None for name in ("torch", "sam2"))


def _assert_fork_safe() -> None:
    """Refuse to run torch in a process forked from a torch-using parent.

    On macOS this combination does not raise — it takes the process out with
    SIGSEGV/SIGABRT inside Metal's shader compiler, which kills an RQ worker
    mid-job and pops a "Python quit unexpectedly" dialog. Measured directly:
    a clean parent forking children that each load torch is fine and repeatable,
    but once the parent itself has run inference, every forked child dies —
    including children that clear the predictor cache and force CPU. The
    poisoning is the fork, not the device.

    So the invariant is "torch is only ever used by a process that has not
    forked from a torch-using parent", and this asserts it rather than trusting
    it. A clear error the operator can act on beats a signal that destroys the
    worker and says nothing.
    """
    global _TORCH_OWNER_PID

    current = os.getpid()
    if _TORCH_OWNER_PID is None:
        _TORCH_OWNER_PID = current
        return
    if _TORCH_OWNER_PID == current:
        return

    raise SegmentationError(
        "Background removal cannot run in this process: it was forked from a "
        "process that had already loaded the segmentation model, which crashes "
        "hard on macOS instead of failing cleanly. Run the worker without "
        "forking (`rq worker --worker-class rq.SimpleWorker`), or set "
        "SEGMENTATION_PROVIDER=http to move the model out of the worker."
    )


def pick_device() -> str:
    """Best available device.

    Explicitly ordered rather than left to a library default, because the
    difference between MPS and CPU here is the difference between a usable tool
    and one nobody will wait for.
    """
    override = os.environ.get("SEGMENTATION_DEVICE", "").strip().lower()
    if override:
        return override

    import torch

    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


@functools.lru_cache(maxsize=4)
def _load_predictor(model_id: str, device: str, owner_pid: int):
    """Loads and caches a predictor.

    Cached per (model, device) because construction downloads and initialises
    weights — doing that per click would make the tool unusable.

    `owner_pid` is part of the key and is not otherwise used. An lru_cache
    survives fork, so without it a forked child would silently reuse a predictor
    holding the parent's GPU handles. `_assert_fork_safe` is the real guard; this
    makes the cache honest on its own terms so it cannot hand out an inherited
    device context if that guard is ever relaxed.
    """
    from sam2.sam2_image_predictor import SAM2ImagePredictor

    return SAM2ImagePredictor.from_pretrained(model_id, device=device)


def segment_at_points(
    frame_rgb: Any,
    points: list[tuple[float, float]],
    labels: list[int],
    *,
    quality: str = "faster",
    box: tuple[float, float, float, float] | None = None,
):
    """Returns a uint8 matte (0..255) for `frame_rgb` given normalised prompts.

    `points` are 0..1 fractions of the frame and are scaled here, so the caller
    never has to know the frame's pixel size — the same reason the selection
    model is stored normalised.
    """
    if not is_installed():
        raise SegmentationError(
            "Point-based selection needs SAM 2, which is not installed on this "
            "server. Run `pip install torch sam2` in the API environment and "
            "restart the worker."
        )

    if not points and box is None:
        raise SegmentationError("Nothing was selected to segment.")

    # Before importing torch, not after: the point is to fail cleanly rather than
    # let a doomed process reach Metal.
    _assert_fork_safe()

    import numpy as np
    import torch

    height, width = frame_rgb.shape[:2]
    device = pick_device()
    predictor = _load_predictor(
        MODEL_IDS.get(quality, MODEL_IDS["faster"]), device, os.getpid()
    )

    point_coords = (
        np.array([[x * width, y * height] for x, y in points], dtype=np.float32)
        if points
        else None
    )
    point_labels = np.array(labels, dtype=np.int32) if points else None

    box_array = None
    if box is not None:
        bx, by, bw, bh = box
        box_array = np.array(
            [bx * width, by * height, (bx + bw) * width, (by + bh) * height],
            dtype=np.float32,
        )

    # autocast on CUDA only. On MPS it currently degrades quality for no real
    # speed gain, and on CPU bfloat16 is slower than fp32.
    context = (
        torch.autocast("cuda", dtype=torch.bfloat16)
        if device == "cuda"
        else torch.inference_mode()
    )

    with torch.inference_mode(), context:
        predictor.set_image(frame_rgb)
        masks, scores, _ = predictor.predict(
            point_coords=point_coords,
            point_labels=point_labels,
            box=box_array,
            multimask_output=True,
        )

    # SAM returns several candidates; take the highest-scoring one. Picking the
    # first would sometimes hand back a sub-part of the subject — a sleeve
    # instead of the person — which reads as the click having missed.
    best = int(np.argmax(scores))
    mask = masks[best]

    return (mask.astype(np.uint8) * 255)
