"""SAM 2.1 video propagation for temporally stable custom cutouts."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .base import SegmentationError, module_available


MODEL_IDS = {
    "faster": "facebook/sam2.1-hiera-tiny",
    "better": "facebook/sam2.1-hiera-large",
}

_PREDICTORS: dict[tuple[str, str, int], Any] = {}
_LOAD_LOCK = threading.Lock()
_INFERENCE_LOCK = threading.Lock()


@dataclass(frozen=True)
class PropagatedMasks:
    directory: Path
    frame_count: int

    def path_for(self, frame_index: int) -> Path:
        return self.directory / f"{frame_index:06d}.png"


def is_installed() -> bool:
    return all(module_available(name) for name in ("torch", "sam2", "cv2"))


def _device() -> str:
    from .sam2_backend import pick_device

    return pick_device()


def _predictor(quality: str):
    from .sam2_backend import _assert_fork_safe

    _assert_fork_safe()
    device = _device()
    model_id = MODEL_IDS.get(quality, MODEL_IDS["faster"])
    key = (model_id, device, os.getpid())
    cached = _PREDICTORS.get(key)
    if cached is not None:
        return cached
    with _LOAD_LOCK:
        cached = _PREDICTORS.get(key)
        if cached is not None:
            return cached
        from sam2.sam2_video_predictor import SAM2VideoPredictor

        # The optional SAM 2 CUDA extension is unavailable on Apple silicon.
        # Disable its tiny-hole post-pass and apply the equivalent bounded
        # OpenCV cleanup in `_write_mask` so MPS jobs stay quiet and deterministic.
        cached = SAM2VideoPredictor.from_pretrained(
            model_id,
            device=device,
            fill_hole_area=0,
        )
        # The Hugging Face Hydra config currently reapplies its checkpoint
        # default after kwargs, so enforce the no-extension path on the instance.
        cached.fill_hole_area = 0
        cached.eval()
        _PREDICTORS[key] = cached
        return cached


def clear_predictors() -> None:
    with _LOAD_LOCK:
        _PREDICTORS.clear()


def _write_mask(path: Path, logits: Any) -> None:
    import cv2  # type: ignore
    import numpy as np  # type: ignore

    array = logits.detach().float().cpu().numpy()
    mask = (np.squeeze(array) > 0.0).astype(np.uint8) * 255
    # Fill only very small enclosed background islands. Large enclosed regions
    # can be real (the space between an arm and torso), so they must survive.
    inverse = (mask == 0).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(inverse, connectivity=8)
    max_hole_area = max(8, int(round(mask.shape[0] * mask.shape[1] * 0.00002)))
    height, width = mask.shape
    for component in range(1, count):
        x, y, component_width, component_height, area = stats[component]
        touches_edge = (
            x == 0
            or y == 0
            or x + component_width >= width
            or y + component_height >= height
        )
        if not touches_edge and area <= max_hole_area:
            mask[labels == component] = 255
    if not cv2.imwrite(str(path), mask):
        raise SegmentationError("Could not store a propagated mask frame.")


def propagate_masks(
    frame_directory: Path,
    *,
    anchor_frame: int,
    quality: str,
    output_directory: Path,
    points: list[tuple[float, float]] | None = None,
    labels: list[int] | None = None,
    anchor_mask: Any = None,
    progress: Any = None,
) -> PropagatedMasks:
    """Tracks one prompted subject in both directions from the anchor frame."""
    if not is_installed():
        raise SegmentationError(
            "Custom video removal needs SAM 2. Run `./scripts/setup_ml_env.sh` "
            "with Python 3.11-3.13, then restart the API and worker."
        )

    frames = sorted(frame_directory.glob("*.jpg"))
    if not frames:
        raise SegmentationError("No clip frames were available for mask propagation.")
    anchor = max(0, min(len(frames) - 1, int(anchor_frame)))
    output_directory.mkdir(parents=True, exist_ok=True)

    import numpy as np  # type: ignore
    import torch

    predictor = _predictor(quality)
    device = _device()

    def context():
        return (
            torch.autocast("cuda", dtype=torch.bfloat16)
            if device == "cuda"
            else torch.inference_mode()
        )

    with _INFERENCE_LOCK, torch.inference_mode(), context():
        state = predictor.init_state(
            video_path=str(frame_directory),
            offload_video_to_cpu=True,
            offload_state_to_cpu=device == "cpu",
            # SAM 2's asynchronous JPEG loader currently divides uint8 frames
            # into NumPy float64 arrays. CUDA accepts the later conversion, but
            # Apple MPS rejects float64 before inference begins. The synchronous
            # path writes into a preallocated float32 tensor, preserving the
            # exact same frames without the unsupported dtype promotion.
            async_loading_frames=device != "mps",
        )
        height = int(state["video_height"])
        width = int(state["video_width"])
        if anchor_mask is not None:
            import cv2  # type: ignore

            composed = np.squeeze(np.asarray(anchor_mask))
            if composed.shape != (height, width):
                composed = cv2.resize(
                    composed.astype(np.uint8),
                    (width, height),
                    interpolation=cv2.INTER_NEAREST,
                )
            composed = np.ascontiguousarray(composed > 0)
            if not np.any(composed):
                raise SegmentationError("The selected subject produced an empty tracking mask.")
            # SAM 2 accepts NumPy here, but allowing its internals to infer the
            # dtype is unsafe on Apple MPS: one inference path promoted the mask
            # to float64, which Metal does not support. Cross the library
            # boundary explicitly as a CPU float32 tensor; SAM moves it to the
            # predictor device itself without changing the precision.
            tracking_mask = torch.from_numpy(composed.astype(np.float32, copy=False))
            _, object_ids, anchor_logits = predictor.add_new_mask(
                inference_state=state,
                frame_idx=anchor,
                obj_id=1,
                mask=tracking_mask,
            )
        else:
            if not points or labels is None or len(points) != len(labels):
                raise SegmentationError("The selected subject has invalid tracking prompts.")
            pixel_points = np.array(
                [[x * width, y * height] for x, y in points], dtype=np.float32
            )
            point_labels = np.asarray(labels, dtype=np.int32)
            _, object_ids, anchor_logits = predictor.add_new_points_or_box(
                inference_state=state,
                frame_idx=anchor,
                obj_id=1,
                points=pixel_points,
                labels=point_labels,
                clear_old_points=True,
            )
        object_index = list(object_ids).index(1)
        _write_mask(output_directory / f"{anchor:06d}.png", anchor_logits[object_index])

        completed: set[int] = {anchor}
        total = len(frames)
        for reverse in (False, True):
            for frame_index, ids, logits in predictor.propagate_in_video(
                state,
                start_frame_idx=anchor,
                reverse=reverse,
            ):
                if frame_index in completed:
                    continue
                ids_list = list(ids)
                if 1 not in ids_list:
                    continue
                _write_mask(output_directory / f"{frame_index:06d}.png", logits[ids_list.index(1)])
                completed.add(int(frame_index))
                if progress:
                    progress(min(78, 14 + int((len(completed) / total) * 64)))

        try:
            predictor.reset_state(state)
        except Exception:
            pass

    if len(completed) != len(frames):
        missing = sorted(set(range(len(frames))) - completed)
        raise SegmentationError(
            f"Mask propagation did not cover {len(missing)} frame(s); first missing frame: {missing[0]}."
        )
    return PropagatedMasks(output_directory, len(frames))
