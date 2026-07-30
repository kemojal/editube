"""Deciding which frames actually need the model.

Segmentation cost is linear in frames and the model is the whole cost — measured
on this machine at ~100ms/frame for SAM 2 tiny and ~166ms for rembg u2netp at
1080p, against 1.6ms to decode a frame and ~20ms to encode one. A 60s clip at
30fps is therefore 3-6 minutes of pure inference, which is what "taking forever"
actually is.

Things that did *not* help, measured rather than assumed:

  * Segmenting at lower resolution. SAM 2 resizes to 1024 internally, so a 1080p
    frame and a 640p frame cost the same (1.0-1.1x); rembg gained only 1.3x.
  * Batching frames through the GPU. On MPS batches of 4 and 8 were *slower*
    per frame than one at a time (0.9x, 0.8x), and 16 collapsed to 5.7s/frame,
    presumably once it stopped fitting.

What does help is not running the model on frames that do not need it. Between two
frames that barely differ, the matte barely differs, so one can be reused or
interpolated. On a synthetic clip with a subject crossing ~160px, striding every
3rd frame with interpolation cut model calls 2.9x while agreeing with the
every-frame result 99.93% of the time (99.72% worst frame).

That measurement is on an easy case — a hard-edged shape on smooth motion. Real
footage has hair, motion blur and occlusion, so a fixed stride would trade
quality invisibly on exactly the shots that need it most. Hence motion-adaptive:
the decision is made from how much the frame actually changed since the last one
the model saw, so a locked-off shot gets a large speedup and a fast pan gets none.
"""

from __future__ import annotations

import os
from typing import Any

#: Mean absolute difference (0..255, on a downscaled grey frame) above which a
#: frame is considered to have moved enough to need its own segmentation.
#: Low deliberately: the cost of a false "needs model" is 100ms, and the cost of
#: a false "reuse" is a visibly wrong matte.
MOTION_THRESHOLD = float(os.environ.get("SEGMENTATION_MOTION_THRESHOLD", "2.0") or "2.0")

#: Never let more than this many frames pass without a real segmentation, however
#: static the shot looks. Drift accumulates, and a slow continuous move can stay
#: under the threshold frame to frame while ending up somewhere else entirely.
MAX_STRIDE = int(os.environ.get("SEGMENTATION_MAX_STRIDE", "4") or "4")

#: Size of the downscaled frame used for the motion estimate. Small on purpose:
#: this must be negligible next to the model, and it is (~0.1ms).
MOTION_PROBE_EDGE = 64


def stride_for_quality(quality: str) -> int:
    """`better` means every frame; `faster` allows the adaptive path.

    Tying this to the existing quality toggle rather than adding another control:
    the user has already said which side of the speed/quality trade they want, and
    a second knob meaning almost the same thing is how inspectors get cluttered.
    """
    return 1 if str(quality or "").strip().lower() == "better" else MAX_STRIDE


def motion_probe(frame_bgr) -> Any:
    """A tiny greyscale thumbnail used only for comparing frames."""
    import cv2  # type: ignore

    grey = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY) if frame_bgr.ndim == 3 else frame_bgr
    return cv2.resize(grey, (MOTION_PROBE_EDGE, MOTION_PROBE_EDGE), interpolation=cv2.INTER_AREA)


def motion_between(previous_probe, current_probe) -> float:
    """Mean absolute difference between two probes, in 0..255."""
    import numpy as np  # type: ignore

    return float(
        np.abs(previous_probe.astype(np.int16) - current_probe.astype(np.int16)).mean()
    )


class StrideDecider:
    """Decides, frame by frame, whether the model has to run.

    Kept as a small object with explicit state rather than a generator so the
    decision is testable on its own — the thing that would silently degrade output
    is this policy, not the loop around it.
    """

    def __init__(self, *, max_stride: int = MAX_STRIDE, threshold: float = MOTION_THRESHOLD):
        # max_stride of 1 disables reuse entirely, which is what `better` wants.
        self.max_stride = max(1, int(max_stride))
        self.threshold = float(threshold)
        self._last_probe = None
        self._since_segment = 0
        self.segmented = 0
        self.reused = 0

    def needs_segmentation(self, probe) -> bool:
        # First frame always runs: there is nothing to reuse.
        if self._last_probe is None:
            self._accept(probe)
            return True

        if self.max_stride == 1:
            self._accept(probe)
            return True

        # Forced refresh, so drift cannot accumulate indefinitely on a slow move
        # that never individually exceeds the threshold.
        if self._since_segment >= self.max_stride - 1:
            self._accept(probe)
            return True

        if motion_between(self._last_probe, probe) >= self.threshold:
            self._accept(probe)
            return True

        self._since_segment += 1
        self.reused += 1
        return False

    def _accept(self, probe) -> None:
        self._last_probe = probe
        self._since_segment = 0
        self.segmented += 1

    @property
    def speedup(self) -> float:
        """Model calls avoided, as a multiple. For logging, not for control flow."""
        total = self.segmented + self.reused
        return (total / self.segmented) if self.segmented else 1.0


def blend_mattes(earlier, later, weight: float):
    """Linear blend between two mattes, `weight` 0 -> earlier, 1 -> later.

    Interpolating rather than holding the previous matte measurably helps on
    motion (99.90% vs 99.84% agreement at stride 4) and costs one lerp, so there
    is no reason to hold.
    """
    import numpy as np  # type: ignore

    if weight <= 0:
        return earlier
    if weight >= 1:
        return later
    mixed = earlier.astype(np.float32) * (1.0 - weight) + later.astype(np.float32) * weight
    return mixed.clip(0, 255).astype(np.uint8)
