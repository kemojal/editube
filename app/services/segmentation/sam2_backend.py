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


def is_installed() -> bool:
    return all(importlib.util.find_spec(name) is not None for name in ("torch", "sam2"))


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


@functools.lru_cache(maxsize=2)
def _load_predictor(model_id: str, device: str):
    """Loads and caches a predictor.

    Cached per (model, device) because construction downloads and initialises
    weights — doing that per click would make the tool unusable. The cache is
    tiny by design: two tiers, one device.
    """
    from sam2.sam2_image_predictor import SAM2ImagePredictor

    predictor = SAM2ImagePredictor.from_pretrained(model_id, device=device)
    return predictor


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

    import numpy as np
    import torch

    if not points and box is None:
        raise SegmentationError("Nothing was selected to segment.")

    height, width = frame_rgb.shape[:2]
    device = pick_device()
    predictor = _load_predictor(MODEL_IDS.get(quality, MODEL_IDS["faster"]), device)

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
