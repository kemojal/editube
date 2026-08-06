"""High-quality automatic foreground matting.

The local provider and the one-frame preview share this module so model choice,
colour conversion, session caching and the optional alpha-matting pass cannot
drift. Importing it is cheap; rembg/onnxruntime are loaded only on first use.
"""

from __future__ import annotations

import os
import threading
from typing import Any

from .base import SegmentationError, module_available


MODEL_IDS = {
    "faster": "u2netp",
    "better": "birefnet-general",
}

_SESSIONS: dict[tuple[str, int], Any] = {}
_SESSION_LOCK = threading.Lock()
_INFERENCE_LOCK = threading.Lock()


def is_installed() -> bool:
    return all(module_available(name) for name in ("rembg", "cv2", "numpy"))


def _session(quality: str):
    model_id = MODEL_IDS.get(quality, MODEL_IDS["faster"])
    key = (model_id, os.getpid())
    cached = _SESSIONS.get(key)
    if cached is not None:
        return cached

    with _SESSION_LOCK:
        cached = _SESSIONS.get(key)
        if cached is not None:
            return cached
        from rembg import new_session  # type: ignore

        cached = new_session(model_id)
        _SESSIONS[key] = cached
        return cached


def clear_sessions() -> None:
    with _SESSION_LOCK:
        _SESSIONS.clear()


def segment_auto(frame_bgr: Any, *, quality: str = "faster"):
    """Returns a uint8 0..255 matte for one OpenCV BGR frame.

    Better mode asks rembg for its alpha-matted RGBA result and extracts the
    alpha channel. That is slower than `only_mask=True`, but it preserves hair,
    fur and motion-blurred edges that a binary silhouette cannot represent.
    """
    if not is_installed():
        raise SegmentationError(
            "Automatic background removal is not installed. Use Python 3.11-3.13 "
            "and run `./scripts/setup_ml_env.sh`, then restart the API and worker."
        )

    import cv2  # type: ignore
    import numpy as np  # type: ignore
    from rembg import remove  # type: ignore

    tier = "better" if str(quality).strip().lower() == "better" else "faster"
    # OpenCV is BGR and the model is trained on RGB. Feeding the channels in the
    # wrong order does not crash; it just quietly costs segmentation accuracy.
    frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)

    with _INFERENCE_LOCK:
        if tier == "better":
            result = remove(
                frame_rgb,
                session=_session(tier),
                only_mask=False,
                alpha_matting=True,
                alpha_matting_foreground_threshold=235,
                alpha_matting_background_threshold=15,
                alpha_matting_erode_size=8,
            )
        else:
            result = remove(frame_rgb, session=_session(tier), only_mask=True)

    array = np.asarray(result)
    if array.ndim == 3:
        # Alpha-matted output is RGBA. Be defensive for a backend returning RGB
        # and fall back to a luminance mask rather than indexing past the shape.
        matte = array[:, :, 3] if array.shape[2] >= 4 else cv2.cvtColor(array, cv2.COLOR_RGB2GRAY)
    else:
        matte = array
    return np.ascontiguousarray(matte.clip(0, 255).astype(np.uint8))


def fuse_identity_with_detail(identity_matte: Any, detail_matte: Any):
    """Uses a tracked SAM mask for identity and BiRefNet for soft edge detail.

    The eroded tracked core is always retained. BiRefNet contributes hair and
    translucent boundaries only inside a lightly dilated tracking gate, so it
    cannot jump to a different salient object elsewhere in the frame.
    """
    import cv2  # type: ignore
    import numpy as np  # type: ignore

    if identity_matte.shape != detail_matte.shape:
        detail_matte = cv2.resize(
            detail_matte,
            (identity_matte.shape[1], identity_matte.shape[0]),
            interpolation=cv2.INTER_LINEAR,
        )
    shorter = max(1, min(identity_matte.shape[:2]))
    radius = max(1, int(round(shorter * 0.006)))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (radius * 2 + 1, radius * 2 + 1))
    gate = cv2.dilate(identity_matte, kernel)
    core = cv2.erode(identity_matte, kernel)
    detailed = np.minimum(detail_matte, gate)
    return np.maximum(core, detailed).astype(np.uint8)
