"""Mask tracking via SAM2 video propagation — the semantic tracking backend.

The plan's G7 remedy of first choice (docs/editing-harness-implementation-plan.md
§5.2): the pinned OpenCV build has no CSRT, but the segmentation stack already
ships a bidirectional SAM2 video predictor that background removal uses. This
module points it at the mask-tracking problem: seed the subject from the
mask's own box at the anchor frame, propagate in both directions, and turn
each propagated mask into the same percent-of-frame transform keyframes the
CSRT path emits — the frontend cannot tell the backends apart.

Split the way the harness splits everything: `mask_bboxes_to_keyframes` is
pure (frame boxes in, keyframes out — where "lost" starts, stride budgets,
direction bounds) and unit-tested without torch; `track_by_propagation` is
the glue that needs the ML stack and only runs where
`segmentation.video_backend.is_installed()` says it can.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any, Callable

from app.jobs.mask_track import bbox_to_transform, keyframe_stride

logger = logging.getLogger(__name__)

#: A propagated mask covering less of the frame than this is "lost" — SAM2
#: rarely returns truly empty masks for a tracked subject that vanished; it
#: returns slivers.
MIN_MASK_COVERAGE = 0.0005


def propagation_available() -> bool:
    try:
        from app.services.segmentation import video_backend

        return bool(video_backend.is_installed())
    except Exception:  # noqa: BLE001
        return False


def bbox_from_mask_file(path: Path) -> tuple[float, float, float, float] | None:
    """Bounding box (x, y, w, h) of the white region of a mask PNG, or None."""
    import cv2
    import numpy as np

    mask = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if mask is None or mask.size == 0:
        return None
    ys, xs = np.where(mask > 127)
    if xs.size == 0 or xs.size < mask.size * MIN_MASK_COVERAGE:
        return None
    x0, x1 = float(xs.min()), float(xs.max())
    y0, y1 = float(ys.min()), float(ys.max())
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1 - x0 + 1, y1 - y0 + 1)


def mask_bboxes_to_keyframes(
    bboxes: list[tuple[float, float, float, float] | None],
    *,
    fps: float,
    clip_start: float,
    anchor_index: int,
    frame_size: tuple[int, int],
    direction: str = "both",
    source_frame_offset: int = 0,
    budget: int | None = None,
) -> tuple[list[dict[str, Any]], int | None]:
    """Propagated per-frame boxes → transform keyframes, with loss semantics.

    `bboxes[i]` is the box on extracted frame `i` (clip-relative); the anchor
    frame always emits. Walking outward from the anchor in each requested
    direction, the first `None` stops that direction — and a forward loss is
    reported as `lost_at` (a source-absolute frame index, matching the CSRT
    path) so the UI's Keep/Discard flow behaves identically. Keyframes beyond
    the loss point are never emitted. A stride keeps the emitted count inside
    the same budget the box tracker uses.
    """
    total = len(bboxes)
    if total == 0 or not (0 <= anchor_index < total):
        return [], None
    stride = keyframe_stride(total, budget) if budget else keyframe_stride(total)

    def make(index: int, bbox: tuple[float, float, float, float]) -> dict[str, Any]:
        frame_idx = source_frame_offset + index
        return {
            "frame": frame_idx,
            "t": max(0.0, (frame_idx / fps if fps > 0 else 0.0) - clip_start),
            **bbox_to_transform(bbox, frame_size),
            "rotation": 0.0,
        }

    keyframes: list[dict[str, Any]] = []
    lost_at: int | None = None

    anchor_bbox = bboxes[anchor_index]
    if anchor_bbox is not None:
        keyframes.append(make(anchor_index, anchor_bbox))

    if direction in ("forward", "both"):
        steps = 0
        for index in range(anchor_index + 1, total):
            bbox = bboxes[index]
            if bbox is None:
                lost_at = source_frame_offset + index
                break
            steps += 1
            if steps % stride == 0 or index == total - 1:
                keyframes.append(make(index, bbox))

    if lost_at is None and direction in ("backward", "both"):
        steps = 0
        for index in range(anchor_index - 1, -1, -1):
            bbox = bboxes[index]
            if bbox is None:
                lost_at = source_frame_offset + index
                break
            steps += 1
            if steps % stride == 0 or index == 0:
                keyframes.append(make(index, bbox))

    keyframes.sort(key=lambda kf: kf["frame"])
    return keyframes, lost_at


def track_by_propagation(
    source: str,
    *,
    mask_bbox_normalized: dict[str, Any],
    clip_start: float,
    clip_end: float,
    anchor_time: float,
    direction: str,
    quality: str = "faster",
    progress: Callable[[int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run the SAM2 propagation pipeline over the clip's range.

    Returns `{"keyframes": [...], "lostAtFrame": int | None, "fps": float}`
    with frame indices SOURCE-absolute, exactly like `_track_direction`.
    Raises `SegmentationError` with a user-facing sentence on any ML failure.
    """
    from app.jobs.mask_track import transform_to_bbox
    from app.services.segmentation import sam2_backend, video_backend
    from app.services.segmentation.local import _extract_tracking_frames

    import cv2

    probe = cv2.VideoCapture(source)
    try:
        fps = float(probe.get(cv2.CAP_PROP_FPS)) or 30.0
    finally:
        probe.release()

    clip_seconds = max(0.2, clip_end - clip_start)
    expected = max(2, int(round(clip_seconds * fps)))

    with tempfile.TemporaryDirectory() as tmp:
        frame_directory = Path(tmp) / "frames"
        frame_directory.mkdir(parents=True)
        saved = _extract_tracking_frames(
            source,
            frame_directory,
            start=clip_start,
            clip_seconds=clip_seconds,
            size=None,
            expected=expected,
            progress=(lambda value: progress(int(8 + value * 0.1))) if progress else None,
        )
        if saved <= 0:
            from app.services.segmentation.base import SegmentationError

            raise SegmentationError("No frames could be read from the clip's range.")
        if cancelled and cancelled():
            return {"keyframes": [], "lostAtFrame": None, "fps": fps, "cancelled": True}

        first = cv2.imread(str(frame_directory / f"{0:06d}.jpg"))
        if first is None:
            from app.services.segmentation.base import SegmentationError

            raise SegmentationError("Could not read the clip's first frame.")
        extracted_size = (int(first.shape[1]), int(first.shape[0]))

        anchor_index = max(0, min(saved - 1, int(round(anchor_time * fps))))
        anchor_bgr = cv2.imread(str(frame_directory / f"{anchor_index:06d}.jpg"))
        if anchor_bgr is None:
            from app.services.segmentation.base import SegmentationError

            raise SegmentationError("Could not read the anchor frame for tracking.")

        # Seed from the mask's own box: its centre as the positive prompt.
        x, y, w, h = transform_to_bbox(mask_bbox_normalized, extracted_size)
        centre = (x + w / 2, y + h / 2)
        anchor_mask = sam2_backend.segment_prompt_groups(
            cv2.cvtColor(anchor_bgr, cv2.COLOR_BGR2RGB),
            [[centre]],
            [],
            quality=quality,
        )

        propagated = video_backend.propagate_masks(
            frame_directory,
            anchor_frame=anchor_index,
            anchor_mask=anchor_mask,
            quality=quality,
            output_directory=Path(tmp) / "sam2-masks",
            progress=(lambda value: progress(int(20 + value * 0.6))) if progress else None,
        )

        bboxes: list[tuple[float, float, float, float] | None] = []
        for index in range(propagated.frame_count):
            if cancelled and cancelled() and index % 30 == 0:
                return {"keyframes": [], "lostAtFrame": None, "fps": fps, "cancelled": True}
            bboxes.append(bbox_from_mask_file(propagated.path_for(index)))

        source_offset = int(round(clip_start * fps))
        keyframes, lost_at = mask_bboxes_to_keyframes(
            bboxes,
            fps=fps,
            clip_start=clip_start,
            anchor_index=anchor_index,
            frame_size=extracted_size,
            direction=direction,
            source_frame_offset=source_offset,
        )
        if progress:
            progress(90)
        return {"keyframes": keyframes, "lostAtFrame": lost_at, "fps": fps}
