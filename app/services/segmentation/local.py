"""In-process provider — the default while there is no GPU service.

Matting runs frame by frame through `rembg`, which wraps ONNX models (u2net by
default, BiRefNet available) and needs only onnxruntime rather than a full torch
install. That keeps the API image far smaller than a torch dependency would, and
`SEGMENTATION_PROVIDER` still moves the work to a GPU service later without
touching feature code.

Be honest about the tradeoff: on CPU this is a background job with a progress
bar, not an interactive effect. Interactive speed needs the GPU runtime.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .base import (
    CAPABILITY_AUTO_MATTE,
    CAPABILITY_POINT_PROMPT,
    CAPABILITY_PROPAGATE,
    SegmentationError,
    SegmentationResult,
)

#: Frame-by-frame matting is slow on CPU; refuse very long clips rather than
#: appearing to hang for an hour with no way to tell whether it is progressing.
MAX_LOCAL_SECONDS = float(os.environ.get("SEGMENTATION_LOCAL_MAX_SECONDS", "120") or "120")


def _has(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


class LocalSegmentationProvider:
    name = "local"

    #: Maps the quality toggle in the UI onto concrete models. `Faster` is the
    #: lighter u2net; `Better` is BiRefNet, which holds hair and soft edges far
    #: better and costs proportionally more.
    MODELS = {"faster": "u2netp", "better": "birefnet-general"}

    def supports(self, capability: str) -> bool:
        """rembg does whole-subject matting only.

        Point prompts and cross-frame propagation need a promptable video model
        (SAM 2), which is a separate install. Reporting this honestly is what
        lets the editor hide a subject picker it cannot honour rather than
        showing one that silently does nothing.
        """
        if capability == CAPABILITY_AUTO_MATTE:
            return _has("rembg")
        if capability in (CAPABILITY_POINT_PROMPT, CAPABILITY_PROPAGATE):
            return _has("sam2")
        return False

    def is_available(self) -> tuple[bool, str]:
        missing = [name for name in ("rembg", "cv2", "numpy", "PIL") if not _has(name)]
        if missing:
            return False, (
                "Background removal is not installed on this server. Run "
                "`pip install -r requirements-ml.txt` in the API environment, "
                f"then restart the worker. Missing: {', '.join(missing)}."
            )
        return True, ""

    def run_effect(
        self,
        source: str,
        effect_type: str,
        clip_target: dict[str, Any],
        settings: dict[str, Any],
        *,
        output_dir: Path,
        progress: Any = None,
    ) -> SegmentationResult:
        ready, reason = self.is_available()
        if not ready:
            raise SegmentationError(reason)

        if effect_type != "remove_bg":
            raise SegmentationError(
                f"{effect_type.replace('_', ' ')} is not available on this server yet."
            )

        return self._remove_background(source, settings, output_dir, progress=progress)

    def _remove_background(
        self,
        source: str,
        settings: dict[str, Any],
        output_dir: Path,
        *,
        progress: Any = None,
    ) -> SegmentationResult:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
        from rembg import new_session, remove  # type: ignore

        quality = str(settings.get("quality") or "faster").lower()
        session = new_session(self.MODELS.get(quality, self.MODELS["faster"]))

        capture = cv2.VideoCapture(source)
        if not capture.isOpened():
            raise SegmentationError("Could not read the video for background removal.")

        fps = capture.get(cv2.CAP_PROP_FPS) or 25.0
        total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)

        if total and fps and total / fps > MAX_LOCAL_SECONDS:
            capture.release()
            raise SegmentationError(
                f"This clip is longer than the {int(MAX_LOCAL_SECONDS)}s limit for "
                "on-server background removal. Trim it, or configure a GPU provider."
            )

        # The matte is scratch; the cutout goes to output_dir for the job to publish.
        with tempfile.TemporaryDirectory() as tmp:
            # VP9 in WebM, because the alpha channel has to survive — an mp4
            # here would silently composite the matte onto black.
            matte_path = Path(tmp) / "matte.webm"
            writer = cv2.VideoWriter(
                str(matte_path),
                cv2.VideoWriter_fourcc(*"VP90"),
                fps,
                (width, height),
            )
            if not writer.isOpened():
                capture.release()
                raise SegmentationError(
                    "This server's OpenCV build cannot write VP9, which is needed to "
                    "keep transparency. Install ffmpeg support or use a GPU provider."
                )

            index = 0
            try:
                while True:
                    ok, frame = capture.read()
                    if not ok:
                        break

                    cut = remove(frame, session=session, only_mask=True)
                    # Write the matte as greyscale RGB; the alpha is muxed in
                    # afterwards, since VideoWriter has no alpha channel.
                    writer.write(cv2.cvtColor(np.asarray(cut), cv2.COLOR_GRAY2BGR))

                    index += 1
                    if progress and total:
                        progress(min(92, 10 + int((index / total) * 80)))
            finally:
                writer.release()
                capture.release()

            output = output_dir / "cutout.webm"
            # Combine colour and matte into one alpha video. `alphamerge` is the
            # step that makes the result a cutout rather than a mask preview.
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-i", source,
                    "-i", str(matte_path),
                    "-filter_complex", "[0:v][1:v]alphamerge[out]",
                    "-map", "[out]",
                    "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
                    str(output),
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            return SegmentationResult(path=output)
