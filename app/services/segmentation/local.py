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

import os
import subprocess
from pathlib import Path
from typing import Any

from .base import (
    CAPABILITY_AUTO_MATTE,
    CAPABILITY_POINT_PROMPT,
    CAPABILITY_PROPAGATE,
    SegmentationError,
    SegmentationResult,
    module_available,
)
from .matte_ops import matte_settings_from_attributes, refine_matte
from .matte_stride import StrideDecider, blend_mattes, motion_probe, stride_for_quality
from .chroma_matte import chroma_keep_matte, combine_keep_mattes

#: Frame-by-frame matting is slow on CPU; refuse very long clips rather than
#: appearing to hang for an hour with no way to tell whether it is progressing.
MAX_LOCAL_SECONDS = float(os.environ.get("SEGMENTATION_LOCAL_MAX_SECONDS", "120") or "120")
TRACKING_MAX_EDGE = max(
    256,
    min(2048, int(os.environ.get("SEGMENTATION_TRACK_MAX_EDGE", "1024") or "1024")),
)


def _has(module: str) -> bool:
    return module_available(module)


class LocalSegmentationProvider:
    name = "local"

    def supports(self, capability: str) -> bool:
        """rembg does whole-subject matting only.

        Point prompts and cross-frame propagation need a promptable video model
        (SAM 2), which is a separate install. Reporting this honestly is what
        lets the editor hide a subject picker it cannot honour rather than
        showing one that silently does nothing.
        """
        if capability == CAPABILITY_AUTO_MATTE:
            from . import auto_backend

            return auto_backend.is_installed()
        if capability == CAPABILITY_POINT_PROMPT:
            from . import sam2_backend

            return sam2_backend.is_installed()
        if capability == CAPABILITY_PROPAGATE:
            from . import video_backend

            return video_backend.is_installed()
        return False

    def is_available(self) -> tuple[bool, str]:
        missing = [name for name in ("cv2", "numpy", "PIL") if not _has(name)]
        if missing or not (self.supports(CAPABILITY_AUTO_MATTE) or self.supports(CAPABILITY_POINT_PROMPT)):
            return False, (
                "Background removal is not ready. Use Python 3.11-3.13, run "
                "`./scripts/setup_ml_env.sh`, then restart the API and worker."
                + (f" Missing: {', '.join(missing)}." if missing else "")
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
        if effect_type != "remove_bg":
            raise SegmentationError(
                f"{effect_type.replace('_', ' ')} is not available on this server yet."
            )

        custom = bool(settings.get("customRemoval")) and self._selection_prompts(settings) is not None
        required = CAPABILITY_POINT_PROMPT if custom else CAPABILITY_AUTO_MATTE
        if not self.supports(required):
            if custom:
                raise SegmentationError(
                    "Custom video removal needs Torch and SAM 2. Use Python 3.11-3.13, "
                    "run `./scripts/setup_ml_env.sh`, then restart the API and worker."
                )
            raise SegmentationError(
                "Automatic background removal needs rembg and OpenCV. Use Python 3.11-3.13, "
                "run `./scripts/setup_ml_env.sh`, then restart the API and worker."
            )

        return self._remove_background(source, clip_target, settings, output_dir, progress=progress)

    @staticmethod
    def _selection_prompts(settings: dict[str, Any]):
        """Normalised prompts from the editor's selection, or None.

        Returning None rather than empty lists is what lets the caller choose
        between prompted and automatic matting on one condition.
        """
        selection = settings.get("selection") or {}
        raw_points = selection.get("points") or []
        strokes = selection.get("strokes") or []

        points: list[tuple[float, float]] = []
        labels: list[int] = []

        for point in raw_points:
            if isinstance(point, dict) and "x" in point and "y" in point:
                points.append((float(point["x"]), float(point["y"])))
                labels.append(1 if point.get("include", True) else 0)

        # A brush stroke becomes a run of point prompts. Sampling rather than
        # sending every vertex keeps the prompt count sane on a long stroke,
        # which SAM is sensitive to.
        for stroke in strokes:
            stroke_points = stroke.get("points") or []
            label = 1 if stroke.get("include", True) else 0
            step = max(1, len(stroke_points) // 12)
            for vertex in stroke_points[::step]:
                if isinstance(vertex, dict) and "x" in vertex and "y" in vertex:
                    points.append((float(vertex["x"]), float(vertex["y"])))
                    labels.append(label)

        if not points:
            return None
        return points, labels

    def _remove_background(
        self,
        source: str,
        clip_target: dict[str, Any],
        settings: dict[str, Any],
        output_dir: Path,
        *,
        progress: Any = None,
    ) -> SegmentationResult:
        """Dispatches to a spawned process unless isolation is switched off.

        The indirection is not architectural taste — it is the fix for three
        distinct hard crashes. An RQ worker forks per job, and a forked child on
        macOS cannot safely reach SystemConfiguration (which any HTTP client
        does, including a HuggingFace cache check), the MPS allocator, or the
        Metal compiler. See `isolated.py` for the crash signatures.
        """
        from .isolated import isolation_enabled, remove_background_isolated

        if isolation_enabled():
            return remove_background_isolated(
                source, clip_target, settings, output_dir, progress=progress
            )
        return self.remove_background_inprocess(
            source, clip_target, settings, output_dir, progress=progress
        )

    def remove_background_inprocess(
        self,
        source: str,
        clip_target: dict[str, Any],
        settings: dict[str, Any],
        output_dir: Path,
        *,
        progress: Any = None,
    ) -> SegmentationResult:
        """The actual work. Public because the spawned child calls it by name.

        Safe to call directly only from a process that has not forked from a
        torch-using or HTTP-using parent — uvicorn, or the spawned child itself.
        """
        # Report before the slow imports, not after. Loading torch and the model
        # weights takes several seconds during which nothing else happens, and a
        # bar that sits at one number through it is indistinguishable from a
        # wedged job — which is exactly how a genuinely dead job was read.
        def step(value: int) -> None:
            if progress:
                progress(value)

        step(4)

        import cv2  # type: ignore
        import numpy as np  # type: ignore

        step(6)

        quality = str(settings.get("quality") or "faster").lower()
        # Read once per clip, not per frame: the interactive preview reads the same
        # settings through the same function, which is what keeps what the user
        # tuned on screen identical to what the render produces.
        refine = matte_settings_from_attributes(settings)

        # Prompted segmentation when the user has selected something, automatic
        # salient-subject matting otherwise. This is what makes the clicks steer
        # the result rather than merely being recorded.
        prompts = self._selection_prompts(settings)
        use_prompts = prompts is not None and bool(settings.get("customRemoval"))

        step(8)

        capture = cv2.VideoCapture(source)
        if not capture.isOpened():
            raise SegmentationError("Could not read the video for background removal.")

        fps = capture.get(cv2.CAP_PROP_FPS) or 25.0
        source_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)

        # Only the clip's own range needs matting. Reading the whole source was
        # both enormously wasteful and the reason a five-second clip taken from a
        # long recording was refused: the limit was being measured against the
        # source, not the clip.
        start = max(0.0, float(clip_target.get("start") or 0.0))
        end = float(clip_target.get("end") or 0.0)
        clip_seconds = (end - start) if end > start else (source_frames / fps if fps else 0.0)

        if clip_seconds > MAX_LOCAL_SECONDS:
            capture.release()
            raise SegmentationError(
                f"This clip is {int(clip_seconds)}s, over the "
                f"{int(MAX_LOCAL_SECONDS)}s limit for on-server background removal. "
                "Trim the clip, raise SEGMENTATION_LOCAL_MAX_SECONDS, or configure a "
                "GPU provider."
            )

        total = int(round(clip_seconds * fps)) if fps else 0
        if start > 0:
            capture.set(cv2.CAP_PROP_POS_MSEC, start * 1000.0)

        if not width or not height:
            capture.release()
            raise SegmentationError("Could not determine the video's dimensions.")

        output = output_dir / "cutout.webm"

        propagated = None
        frame_directory = output_dir / "sam2-frames"
        if use_prompts:
            from . import video_backend

            frame_directory.mkdir(parents=True, exist_ok=True)
            tracking_scale = min(1.0, TRACKING_MAX_EDGE / max(width, height))
            tracking_size = (
                max(1, int(round(width * tracking_scale))),
                max(1, int(round(height * tracking_scale))),
            )
            saved = 0
            while True:
                if total and saved >= total:
                    break
                ok, frame = capture.read()
                if not ok:
                    break
                tracking_frame = (
                    cv2.resize(frame, tracking_size, interpolation=cv2.INTER_AREA)
                    if tracking_scale < 1.0
                    else frame
                )
                if not cv2.imwrite(
                    str(frame_directory / f"{saved:06d}.jpg"),
                    tracking_frame,
                    [cv2.IMWRITE_JPEG_QUALITY, 92],
                ):
                    capture.release()
                    raise SegmentationError("Could not prepare frames for custom background removal.")
                saved += 1
                if progress and total:
                    progress(min(13, 8 + int((saved / total) * 5)))
            capture.release()
            if saved == 0:
                raise SegmentationError("No frames could be read from the clip's range.")
            total = saved
            anchor_seconds = float(settings.get("selectionAnchorSeconds") or start)
            anchor_frame = round((anchor_seconds - start) * fps)
            assert prompts is not None
            propagated = video_backend.propagate_masks(
                frame_directory,
                anchor_frame=anchor_frame,
                points=prompts[0],
                labels=prompts[1],
                quality=quality,
                output_directory=output_dir / "sam2-masks",
                progress=progress,
            )

        # The matte is piped straight into ffmpeg as raw greyscale frames rather
        # than written to an intermediate video.
        #
        # The previous version went through cv2.VideoWriter with a "VP90" fourcc.
        # That was wrong twice over: the matte carries no alpha of its own — it
        # *is* the alpha, one grey plane — so an alpha-capable codec bought
        # nothing, and OpenCV rejected the fourcc anyway ("tag 0x30395056/'VP90'
        # is not supported"), silently fell back to another encoder, and still
        # returned isOpened() == True, so the guard against exactly that was dead
        # code. Piping removes the codec lottery, keeps the matte lossless
        # instead of re-compressing a mask, and drops a whole decode pass.
        command = [
            "ffmpeg", "-y",
            # Trim the colour input to the clip. A mismatch between this range
            # and the frames read below silently offsets the cutout.
            *(["-ss", f"{start:.3f}"] if start > 0 else []),
            *(["-t", f"{clip_seconds:.3f}"] if clip_seconds > 0 else []),
            "-i", source,
            # The matte, arriving on stdin.
            "-f", "rawvideo",
            "-pix_fmt", "gray",
            "-s", f"{width}x{height}",
            "-r", f"{fps:.6f}",
            "-i", "-",
            "-filter_complex",
            "[0:v][1:v]alphamerge"
            + (
                f",despill=type=green:mix={max(0.0, min(1.0, float(settings.get('spill') or 0.0))):.4f}"
                if settings.get("chromaKey") and float(settings.get("spill") or 0.0) > 0
                else ""
            )
            + "[out]",
            "-map", "[out]",
            # VP9/WebM is what a browser can actually play with transparency.
            # Note for anyone verifying this file: ffmpeg's *native* vp9 decoder
            # drops the alpha layer, so `ffmpeg -i cutout.webm` reports yuv420p
            # and looks like the alpha was lost. It is there — the WebM carries
            # alpha_mode=1. Decode with `-c:v libvpx-vp9` to see it.
            "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
            "-b:v", "0", "-crf", "32",
            # libvpx-vp9 defaults to a very slow single-threaded encode. Measured
            # 3x faster at 1080p with these, for a few percent more bytes on a
            # matte-driven cutout — an unambiguous trade here.
            "-row-mt", "1",
            "-threads", str(max(2, min(8, (os.cpu_count() or 4)))),
            "-tile-columns", "2",
            "-cpu-used", "5",
            "-deadline", "good",
            str(output),
        ]

        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        assert process.stdin is not None

        def finish_encoder() -> bytes:
            """Close the raw-video pipe without asking communicate() to flush it.

            Python's `communicate()` flushes stdin when it starts; calling it
            after we closed the pipe raises `ValueError: flush of closed file`
            and turns a valid render into a failed job.
            """
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            process.stdin = None
            stderr = process.stderr.read() if process.stderr is not None else b""
            process.wait()
            return stderr

        def segment(frame):
            """Runs the chosen model on one frame and normalises the result."""
            from .auto_backend import segment_auto

            cut = segment_auto(frame, quality=quality)

            matte = np.ascontiguousarray(np.asarray(cut, dtype=np.uint8))
            if matte.ndim == 3:
                matte = cv2.cvtColor(matte, cv2.COLOR_BGR2GRAY)
            if matte.shape != (height, width):
                # rembg can hand back the model's working size rather than the
                # frame's. A silent mismatch would shear every row.
                matte = cv2.resize(matte, (width, height), interpolation=cv2.INTER_LINEAR)
            return matte

        def emit(matte, frame=None) -> None:
            """Refines and writes one matte. Every frame leaves through here, so
            refinement cannot be applied inconsistently across the reuse path."""
            assert process.stdin is not None
            result = refine_matte(matte, refine)
            if settings.get("chromaKey"):
                if frame is None:
                    raise SegmentationError("A chroma-keyed matte is missing its source frame.")
                result = combine_keep_mattes(result, chroma_keep_matte(frame, settings))
            process.stdin.write(result.tobytes())

        if propagated is not None:
            from .auto_backend import fuse_identity_with_detail, is_installed as auto_installed, segment_auto

            # Tracking frames are intentionally bounded to SAM's useful input
            # resolution. Re-read the original source for final colour, chroma
            # and BiRefNet detail rather than encoding the JPEG proxies.
            source_capture = cv2.VideoCapture(source)
            if not source_capture.isOpened():
                raise SegmentationError("Could not reopen the source for the final cutout pass.")
            if start > 0:
                source_capture.set(cv2.CAP_PROP_POS_MSEC, start * 1000.0)
            try:
                for index in range(propagated.frame_count):
                    ok, frame = source_capture.read()
                    matte = cv2.imread(str(propagated.path_for(index)), cv2.IMREAD_GRAYSCALE)
                    if not ok or frame is None or matte is None:
                        raise SegmentationError(f"Could not read propagated frame {index}.")
                    if matte.shape != (height, width):
                        matte = cv2.resize(
                            matte,
                            (width, height),
                            interpolation=cv2.INTER_LINEAR,
                        )
                    if quality == "better" and auto_installed():
                        matte = fuse_identity_with_detail(
                            matte,
                            segment_auto(frame, quality="better"),
                        )
                    emit(matte, frame)
                    if progress:
                        progress(min(92, 78 + int(((index + 1) / propagated.frame_count) * 14)))
            except BrokenPipeError:
                pass
            finally:
                source_capture.release()
                stderr = finish_encoder()
            if process.returncode != 0:
                raise SegmentationError(
                    "Background removal failed while writing the cutout: "
                    + (stderr.decode("utf-8", "replace").strip().splitlines() or ["unknown error"])[-1]
                )
            return SegmentationResult(path=output)

        # Motion-adaptive segmentation: the model is the entire cost (~100ms/frame
        # against 1.6ms to decode one), so frames that barely changed reuse an
        # interpolated matte instead. See matte_stride.py for what was measured,
        # including the two approaches that did not work.
        # A chroma matte depends on every colour frame, so it cannot use the
        # matte-only interpolation shortcut without retaining those frames.
        decider = StrideDecider(
            max_stride=1 if settings.get("chromaKey") else stride_for_quality(quality)
        )

        index = 0
        # Frames waiting for the *next* segmented matte to interpolate towards.
        # Only the count is needed — the blend is between mattes, not frames — so
        # this holds no image data.
        pending = 0
        previous_matte = None

        try:
            while True:
                if total and index >= total:
                    break
                ok, frame = capture.read()
                if not ok:
                    break

                if decider.needs_segmentation(motion_probe(frame)):
                    matte = segment(frame)
                    # Flush the held frames first, so output stays in frame order:
                    # they sit between the previous segmented matte and this one.
                    if pending and previous_matte is not None:
                        for offset in range(1, pending + 1):
                            emit(blend_mattes(previous_matte, matte, offset / (pending + 1)))
                    elif pending:
                        for _ in range(pending):
                            emit(matte)
                    pending = 0
                    emit(matte, frame)
                    previous_matte = matte
                else:
                    # Held back until the next real segmentation gives us
                    # something to interpolate towards.
                    pending += 1

                index += 1
                if progress and total:
                    progress(min(92, 10 + int((index / total) * 80)))

            # Trailing frames have no later matte to blend with, so they hold the
            # last one. Bounded by max_stride, so this is a few frames at most.
            if pending and previous_matte is not None:
                for _ in range(pending):
                    emit(previous_matte)
                pending = 0
        except BrokenPipeError:
            # ffmpeg died mid-write; its stderr is the useful error, not ours.
            pass
        finally:
            capture.release()
            stderr = finish_encoder()
        if process.returncode != 0:
            raise SegmentationError(
                "Background removal failed while writing the cutout: "
                + (stderr.decode("utf-8", "replace").strip().splitlines() or ["unknown error"])[-1]
            )
        if index == 0:
            raise SegmentationError("No frames could be read from the clip's range.")

        return SegmentationResult(path=output)
