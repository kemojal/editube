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
from .selection_prompts import SelectionPrompts, selection_prompts

#: Frame-by-frame matting is slow on CPU; refuse very long clips rather than
#: appearing to hang for an hour with no way to tell whether it is progressing.
MAX_LOCAL_SECONDS = float(os.environ.get("SEGMENTATION_LOCAL_MAX_SECONDS", "120") or "120")
TRACKING_MAX_EDGE = max(
    256,
    min(2048, int(os.environ.get("SEGMENTATION_TRACK_MAX_EDGE", "1024") or "1024")),
)


class _RawFrameReader:
    """Sequential BGR frames from ffmpeg, one clip range at a time.

    A pipe, not random access: this pass walks the clip forward exactly once, so
    a decoder that can only go forward is all it needs — and it avoids OpenCV's
    per-frame stream handling on a remote source.
    """

    def __init__(self, source: str, start: float, clip_seconds: float, width: int, height: int):
        self._size = width * height * 3
        self._shape = (height, width, 3)
        self._process = subprocess.Popen(
            [
                "ffmpeg", "-nostdin",
                *(["-ss", f"{start:.3f}"] if start > 0 else []),
                "-i", source,
                *(["-t", f"{clip_seconds:.3f}"] if clip_seconds > 0 else []),
                "-f", "rawvideo", "-pix_fmt", "bgr24",
                "-loglevel", "error",
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def read(self):
        """The next frame, or `None` once the stream ends."""
        import numpy as np  # type: ignore

        stream = self._process.stdout
        if stream is None:
            return None
        # `read(n)` on a pipe returns what is available, not what was asked for,
        # so a short read is normal mid-stream and must not be read as EOF —
        # doing that would truncate the render at an arbitrary frame.
        buffer = bytearray()
        while len(buffer) < self._size:
            chunk = stream.read(self._size - len(buffer))
            if not chunk:
                return None
            buffer.extend(chunk)
        # Copied out of the buffer it was read into: the next frame reuses that
        # buffer, and the matting backends hold on to what they are given.
        return np.frombuffer(bytes(buffer), dtype=np.uint8).reshape(self._shape).copy()

    def close(self) -> None:
        if self._process.stdout is not None:
            try:
                self._process.stdout.close()
            except OSError:
                pass
        if self._process.poll() is None:
            self._process.terminate()
        self._process.wait()


def _extract_tracking_frames(
    source: str,
    frame_directory: Path,
    *,
    start: float,
    clip_seconds: float,
    size: tuple[int, int] | None,
    expected: int,
    progress: Any = None,
) -> int:
    """Writes the clip's range as numbered JPEGs and returns how many there are.

    One ffmpeg pass, replacing a per-frame `VideoCapture.read()` + `imwrite()`
    loop. The loop was not slow because of JPEG encoding — it was slow because
    `source` is usually a URL, and OpenCV re-reads and re-seeks that stream frame
    by frame. On an object-store origin that turns "prepare 300 frames" into
    minutes of network, which is exactly where the progress bar was seen sitting
    at 10%. ffmpeg opens the source once, seeks once, and decodes forward.

    `-ss` before `-i` so the seek is a seek rather than a decode of everything
    before the clip; ffmpeg's input seek is accurate by default, so frame 0 here
    is the frame at `start` — the same one the colour pass and the alphamerge
    below start from. Getting that wrong offsets the matte from the picture.
    """
    command = [
        "ffmpeg", "-nostdin", "-y",
        *(["-ss", f"{start:.3f}"] if start > 0 else []),
        "-i", source,
        *(["-t", f"{clip_seconds:.3f}"] if clip_seconds > 0 else []),
        # INTER_AREA's equivalent: the tracking proxy is a downscale, and area
        # is what keeps thin edges from aliasing into the model's input.
        *(["-vf", f"scale={size[0]}:{size[1]}:flags=area"] if size else []),
        "-q:v", "2",
        "-start_number", "0",
        # Frame counts on stdout so the phase can report real progress instead of
        # parking on one number for the whole extraction.
        "-progress", "pipe:1", "-nostats", "-loglevel", "error",
        str(frame_directory / "%06d.jpg"),
    ]

    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    assert process.stdout is not None
    for line in process.stdout:
        if not line.startswith("frame=") or not progress or not expected:
            continue
        try:
            done = int(line.split("=", 1)[1].strip())
        except ValueError:
            continue
        progress(min(13, 8 + int((done / expected) * 5)))
    process.stdout.close()
    stderr = process.stderr.read() if process.stderr is not None else ""
    process.wait()
    if process.returncode != 0:
        tail = (stderr.strip().splitlines() or ["unknown error"])[-1]
        raise SegmentationError(
            "Could not prepare frames for custom background removal: " + tail
        )

    # Count what actually landed rather than trusting the frame-rate arithmetic:
    # a clip that ends early gives fewer frames than `expected`, and tracking
    # then has to iterate over the real number.
    return sum(1 for _ in frame_directory.glob("*.jpg"))


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
    def _selection_prompts(settings: dict[str, Any]) -> SelectionPrompts | None:
        """Compatibility entry point for callers and prompt extraction tests."""
        return selection_prompts(settings)

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
        # Only the automatic path reads frames through this capture. Seeking it
        # is itself a round trip on a remote source, so the prompted path — which
        # hands decoding to ffmpeg below — must not pay for a seek it never uses.
        if start > 0 and not use_prompts:
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
            capture.release()
            saved = _extract_tracking_frames(
                source,
                frame_directory,
                start=start,
                clip_seconds=clip_seconds,
                size=tracking_size if tracking_scale < 1.0 else None,
                expected=total,
                progress=progress,
            )
            if saved == 0:
                raise SegmentationError("No frames could be read from the clip's range.")
            total = saved
            anchor_seconds = float(settings.get("selectionAnchorSeconds") or start)
            anchor_frame = max(0, min(saved - 1, round((anchor_seconds - start) * fps)))
            assert prompts is not None
            anchor_bgr = cv2.imread(str(frame_directory / f"{anchor_frame:06d}.jpg"))
            if anchor_bgr is None:
                raise SegmentationError("Could not read the selected frame for mask tracking.")
            from . import sam2_backend

            anchor_mask = sam2_backend.segment_prompt_groups(
                cv2.cvtColor(anchor_bgr, cv2.COLOR_BGR2RGB),
                prompts.positive_groups,
                prompts.negative_points,
                quality=quality,
            )
            propagated = video_backend.propagate_masks(
                frame_directory,
                anchor_frame=anchor_frame,
                anchor_mask=anchor_mask,
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
            #
            # Through ffmpeg rather than `cv2.VideoCapture`, for the same reason
            # the extraction above is: this is the second full pass over a source
            # that is usually a URL, and OpenCV's per-frame reads over HTTP cost
            # far more than the decode. It also puts every pass in this function
            # on one seek implementation — extraction, this, and the alphamerge
            # encoder all now start at `start` the same way, so the matte cannot
            # drift a frame away from the picture it belongs to.
            source_reader = _RawFrameReader(source, start, clip_seconds, width, height)
            try:
                for index in range(propagated.frame_count):
                    frame = source_reader.read()
                    matte = cv2.imread(str(propagated.path_for(index)), cv2.IMREAD_GRAYSCALE)
                    if frame is None or matte is None:
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
                source_reader.close()
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
