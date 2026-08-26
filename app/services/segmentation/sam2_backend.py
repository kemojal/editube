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

import os
import threading
from collections import OrderedDict
from typing import Any

from .base import SegmentationError, module_available

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
    return all(module_available(name) for name in ("torch", "sam2"))


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


#: Predictors by (model_id, device, pid), and the lock that guards *construction*.
#:
#: This was an `lru_cache`, which was wrong in a way that only appears under
#: concurrency: it does not hold a lock across the call, so N concurrent misses
#: run N constructors. `SAM2Transforms.__init__` calls `torch.jit.script`, which
#: is not re-entrant, so two simultaneous first-clicks corrupted the JIT
#: compilation unit — and corrupted it *permanently*, so every later request in
#: that process failed too:
#:
#:     KeyError: '__torch__.torch.nn.functional.interpolate'
#:     RuntimeError: Can't redefine method: forward on class:
#:         __torch__.torchvision.transforms.transforms.Resize
#:
#: The interactive preview makes this easy to hit: it runs in the API process,
#: where two clicks land on two threads. Reproducible with two threads on a cold
#: cache; not reproducible once warm, which is why an earlier concurrency check
#: passed and this got through.
_PREDICTORS: dict[tuple[str, str, int], Any] = {}
_PREDICTOR_LOCK = threading.Lock()

#: Serialises inference itself, for two independent reasons.
#:
#: 1. Correctness. A SAM 2 predictor is stateful: `set_image` stores the frame's
#:    embeddings *on the predictor*, and `predict` then reads them. Two threads
#:    sharing one predictor can interleave as set(A), set(B), predict(A) — which
#:    silently segments the wrong frame. No error, just a wrong mask.
#: 2. It hangs otherwise. Two concurrent cold requests left one thread wedged
#:    inside MPS `layer_norm` in the image encoder, indefinitely — past the
#:    construction lock and deep in Metal. Flaky rather than deterministic: an
#:    earlier two-thread check on a warm predictor passed, which is exactly why
#:    this needed to be found by hanging rather than by reasoning.
#:
#: The cost is that concurrent previews queue instead of overlapping. At ~300ms
#: each that is barely visible, and it is what a single GPU wants anyway.
_INFERENCE_LOCK = threading.Lock()

#: Image embeddings by (model, device, pid, frame key), newest last.
#:
#: `set_image` runs the image encoder, and that — not the mask decoder — is
#: essentially the whole cost of a click: the decoder is milliseconds, the
#: encoder hundreds of them on tiny and seconds on large. The refine loop asks
#: about one frame repeatedly (click, alt-click, brush, click again), so
#: re-encoding it every time meant the second click cost exactly as much as the
#: first for no new information.
#:
#: What is stored is the predictor's own post-`set_image` state, restored onto
#: it in place of the call. That is safe only because `_INFERENCE_LOCK`
#: serialises everything that reads or writes that state — the same invariant
#: that already made a shared stateful predictor usable at all.
#:
#: Two entries: a 1080p tiny embedding is ~16MB, and the working set is "the
#: frame being refined", not a history.
_EMBEDDINGS: "OrderedDict[tuple[str, str, int, str], dict[str, Any]]" = OrderedDict()
_EMBEDDING_LIMIT = 2


def _set_image_cached(predictor: Any, frame_rgb: Any, cache_key: tuple | None) -> None:
    """`predictor.set_image(frame_rgb)`, skipped when this frame is already encoded.

    Must be called with `_INFERENCE_LOCK` held.
    """
    if cache_key is None:
        predictor.set_image(frame_rgb)
        return

    cached = _EMBEDDINGS.get(cache_key)
    if cached is not None:
        _EMBEDDINGS.move_to_end(cache_key)
        predictor._features = cached["features"]
        predictor._orig_hw = cached["orig_hw"]
        predictor._is_image_set = True
        predictor._is_batch = False
        return

    predictor.set_image(frame_rgb)
    _EMBEDDINGS[cache_key] = {
        "features": predictor._features,
        "orig_hw": predictor._orig_hw,
    }
    while len(_EMBEDDINGS) > _EMBEDDING_LIMIT:
        _EMBEDDINGS.popitem(last=False)


def clear_embeddings() -> None:
    """Drops cached image embeddings. For tests; not part of the request path."""
    with _INFERENCE_LOCK:
        _EMBEDDINGS.clear()


def _load_predictor(model_id: str, device: str, owner_pid: int):
    """Returns a cached predictor, constructing at most one at a time.

    Cached per (model, device, pid) because construction downloads and
    initialises weights — doing that per click would make the tool unusable.

    `owner_pid` is part of the key because a plain cache survives fork, so
    without it a forked child could reuse a predictor holding the parent's GPU
    handles. `_assert_fork_safe` is the real guard; this keeps the cache honest
    on its own terms.

    Double-checked: the fast path stays lock-free once warm, and the slow path
    holds the lock across construction so `torch.jit.script` is never entered
    concurrently. Holding it for the whole load means a second caller waits for
    the first rather than racing it — a few seconds once, against a process that
    otherwise never recovers.
    """
    key = (model_id, device, owner_pid)
    predictor = _PREDICTORS.get(key)
    if predictor is not None:
        return predictor

    with _PREDICTOR_LOCK:
        # Re-check: another thread may have finished while we waited.
        predictor = _PREDICTORS.get(key)
        if predictor is not None:
            return predictor

        from sam2.sam2_image_predictor import SAM2ImagePredictor

        predictor = SAM2ImagePredictor.from_pretrained(model_id, device=device)
        _PREDICTORS[key] = predictor
        return predictor


def clear_predictors() -> None:
    """Drops cached predictors. For tests; not part of the request path."""
    with _PREDICTOR_LOCK:
        _PREDICTORS.clear()
    # Embeddings are keyed by the predictor that produced them and are useless —
    # and, restored onto a fresh predictor, wrong — once it is gone.
    clear_embeddings()


def warm_up(*, quality: str = "faster") -> None:
    """Loads the checkpoint ahead of the first segmentation.

    Exists so the caller can report progress across a multi-second weight load
    rather than having it billed silently to the first frame — where it looked
    exactly like the first frame had hung.
    """
    _assert_fork_safe()
    _load_predictor(MODEL_IDS.get(quality, MODEL_IDS["faster"]), pick_device(), os.getpid())


def segment_at_points(
    frame_rgb: Any,
    points: list[tuple[float, float]],
    labels: list[int],
    *,
    quality: str = "faster",
    box: tuple[float, float, float, float] | None = None,
    frame_key: str | None = None,
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

    # set_image and predict must be one atomic unit — see _INFERENCE_LOCK. The
    # predictor carries the frame's embeddings between the two calls, so letting
    # another thread in between segments the wrong frame.
    model_id = MODEL_IDS.get(quality, MODEL_IDS["faster"])
    cache_key = (
        (model_id, device, os.getpid(), frame_key) if frame_key is not None else None
    )
    with _INFERENCE_LOCK, torch.inference_mode(), context:
        _set_image_cached(predictor, frame_rgb, cache_key)
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


def segment_prompt_groups(
    frame_rgb: Any,
    positive_groups: Any,
    negative_points: Any = (),
    *,
    quality: str = "faster",
    frame_key: str | None = None,
):
    """Segments additive prompt groups independently and unions their mattes.

    SAM interprets several positive points in one prediction as descriptions of
    one object. That is useful for tightening one object, but wrong for the
    editor's Add tool: a second mark is expected to preserve the first region
    and expand the mask. One image embedding is reused for all groups, so this
    has correct additive semantics without re-encoding the frame per mark.
    """
    groups = [tuple(group) for group in positive_groups if group]
    negatives = tuple(negative_points or ())
    if not groups:
        raise SegmentationError("Click the subject to select it first.")
    if not is_installed():
        raise SegmentationError(
            "Point-based selection needs SAM 2, which is not installed on this "
            "server. Run `pip install torch sam2` in the API environment and "
            "restart the worker."
        )

    _assert_fork_safe()

    import numpy as np
    import torch

    height, width = frame_rgb.shape[:2]
    device = pick_device()
    predictor = _load_predictor(
        MODEL_IDS.get(quality, MODEL_IDS["faster"]), device, os.getpid()
    )
    context = (
        torch.autocast("cuda", dtype=torch.bfloat16)
        if device == "cuda"
        else torch.inference_mode()
    )

    model_id = MODEL_IDS.get(quality, MODEL_IDS["faster"])
    cache_key = (
        (model_id, device, os.getpid(), frame_key) if frame_key is not None else None
    )
    union = np.zeros((height, width), dtype=bool)
    with _INFERENCE_LOCK, torch.inference_mode(), context:
        _set_image_cached(predictor, frame_rgb, cache_key)
        for group in groups:
            points = list(group) + list(negatives)
            coords = np.asarray(
                [[x * width, y * height] for x, y in points], dtype=np.float32
            )
            labels = np.asarray(
                [1] * len(group) + [0] * len(negatives), dtype=np.int32
            )
            masks, scores, _ = predictor.predict(
                point_coords=coords,
                point_labels=labels,
                box=None,
                multimask_output=True,
            )
            union |= masks[int(np.argmax(scores))].astype(bool)

    return union.astype(np.uint8) * 255
