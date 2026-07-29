"""RQ job: CV mask tracking. A user places a mask over a moving subject and we
track its bounding box forward/backward across the clip using OpenCV's CSRT
tracker, emitting keyframes in the mask's percent-of-frame transform space.

Pure helpers (`transform_to_bbox`, `bbox_to_transform`, `keyframe_stride`) have
no OpenCV/DB dependency and are covered by `tests/test_mask_track.py`. The job
function itself needs a real video file, a worker and a DB row, so it is only
exercised by reasoning / manual QA, not by the automated suite.
"""

from __future__ import annotations

import ipaddress
import logging
import math
import os
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.db.database import SessionLocal
from app.db.models import AiResult, Video

logger = logging.getLogger(__name__)

# Cap on emitted keyframes for very long clips (10 min @ 30fps = 18000 frames
# would otherwise mean 18000 keyframes).
DEFAULT_KEYFRAME_BUDGET = 120

# Re-check the AiResult row for cancellation this often (frames).
_CANCEL_CHECK_STRIDE = 30


def transform_to_bbox(transform: dict[str, Any], frame_size: tuple[int, int]) -> tuple[float, float, float, float]:
    """Convert a percent-of-frame-from-centre transform to a pixel bbox
    (left, top, width, height). Pure conversion — does NOT clamp to the
    frame, since a mask may legitimately sit partly offscreen and clamping
    would corrupt the round trip through `bbox_to_transform`.
    """
    frame_w, frame_h = frame_size
    x = float(transform.get("x") or 0)
    y = float(transform.get("y") or 0)
    width_pct = float(transform.get("width") or 0)
    height_pct = float(transform.get("height") or 0)

    width = width_pct / 100.0 * frame_w
    height = height_pct / 100.0 * frame_h
    # x/y are percent-of-frame offsets of the box centre from the frame centre.
    center_x = frame_w / 2.0 + (x / 100.0 * frame_w)
    center_y = frame_h / 2.0 + (y / 100.0 * frame_h)
    left = center_x - width / 2.0
    top = center_y - height / 2.0
    return left, top, width, height


def bbox_to_transform(bbox: tuple[float, float, float, float], frame_size: tuple[int, int]) -> dict[str, float]:
    """Inverse of `transform_to_bbox`. Pure conversion — no clamping."""
    left, top, width, height = bbox
    frame_w, frame_h = frame_size
    center_x = left + width / 2.0
    center_y = top + height / 2.0
    x = (center_x - frame_w / 2.0) / frame_w * 100.0
    y = (center_y - frame_h / 2.0) / frame_h * 100.0
    width_pct = width / frame_w * 100.0
    height_pct = height / frame_h * 100.0
    return {"x": x, "y": y, "width": width_pct, "height": height_pct}


def keyframe_stride(total_frames: int, budget: int = DEFAULT_KEYFRAME_BUDGET) -> int:
    """Frame stride so that at most `budget` keyframes are emitted across
    `total_frames`. Never zero (guards div-by-zero / degenerate inputs)."""
    total = max(0, int(total_frames or 0))
    if total <= 0:
        return 1
    return max(1, math.ceil(total / budget))


class UnsafeMediaSourceError(RuntimeError):
    """Raised when a media source fails the SSRF allowlist. Never includes
    the rejected URL/host in its message -- callers must not echo the
    resolved source back to the client (I7)."""


_ALLOWED_SCHEMES = {"http", "https"}


def _is_disallowed_host(hostname: str) -> bool:
    """True if `hostname` resolves to (or literally is) a loopback,
    link-local, private, or otherwise non-public address -- the classes an
    SSRF probe targets (e.g. cloud metadata at 169.254.169.254, internal
    services on 10.x/192.168.x, or the worker's own loopback)."""
    candidates: list[str] = [hostname]
    try:
        # Resolve DNS names too -- a public-looking hostname can still
        # resolve to an internal address ("DNS rebinding").
        infos = socket.getaddrinfo(hostname, None)
        candidates.extend(sorted({info[4][0] for info in infos}))
    except socket.gaierror:
        pass

    for candidate in candidates:
        try:
            ip = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return True
    return False


def _assert_media_source_safe(source: str) -> None:
    """Guards `cv2.VideoCapture(source)` against SSRF (I7): only http(s)
    URLs to public hosts are allowed. Local filesystem paths (no scheme --
    already resolved by `_resolve_media_source` from trusted server-side
    state, never directly from client input) pass through untouched."""
    if "://" not in source:
        return
    parsed = urlparse(source)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UnsafeMediaSourceError("Unsupported media source scheme.")
    hostname = parsed.hostname
    if not hostname or _is_disallowed_host(hostname):
        raise UnsafeMediaSourceError("Media source host is not allowed.")


def _resolve_media_source(video: Video, explicit_source_url: str | None) -> str:
    """Resolve the actual media stream to run OpenCV over.

    Priority:
    1. An explicit `source_url` supplied with the request (timeline clips
       carry their own source, e.g. dropped/repurposed clips).
    2. The same resolution the transcription worker uses for YouTube-ingested
       rows: the canonical watch URL (`video.ingest_page_url`), NOT
       `video.file_path`, which is often a video-only DASH URL.
    3. A local/uploaded file path fallback.
    """
    explicit = (explicit_source_url or "").strip()
    if explicit:
        return explicit

    page_for_video = (video.ingest_page_url or "").strip()
    if page_for_video:
        try:
            from app.services.youtube_stream_resolve import (
                YoutubeStreamResolveError,
                resolve_youtube_page_to_stream_url,
            )

            return resolve_youtube_page_to_stream_url(page_for_video)
        except Exception as exc:
            logger.warning(
                "mask_track: could not resolve YouTube stream for video %s (falling back to file_path): %s",
                video.id,
                exc,
            )

    value = (video.file_path or "").strip()
    if value.startswith(("http://", "https://")):
        return value
    if value.startswith("/uploads/"):
        candidate = Path(os.environ.get("UPLOADS_DIR", "./uploads")).resolve() / value.removeprefix("/uploads/")
        if candidate.exists():
            return str(candidate)
    candidate = Path(value)
    if candidate.exists():
        return str(candidate)
    return value


def _box_left_frame(bbox: tuple[float, float, float, float], frame_size: tuple[int, int]) -> bool:
    """True when the box has left the frame by more than its own size."""
    left, top, width, height = bbox
    frame_w, frame_h = frame_size
    if width <= 0 or height <= 0:
        return True
    right = left + width
    bottom = top + height
    # "Left the frame by more than its own size" -> the box is entirely
    # outside the frame plus one extra box-size margin on the exit side.
    if right < -width or left > frame_w + width:
        return True
    if bottom < -height or top > frame_h + height:
        return True
    return False


def _row_is_stopped(db, ai_result_id: int) -> bool:
    """Re-read the row; True if it flipped away from queued/processing
    (i.e. cancelled/failed) meaning the worker should stop."""
    db.expire_all()
    row = db.query(AiResult).filter(AiResult.id == ai_result_id).first()
    if row is None:
        return True
    return row.status not in ("queued", "processing")


def _update_progress(db, ai_result_id: int, *, progress: int) -> None:
    row = db.query(AiResult).filter(AiResult.id == ai_result_id).first()
    if row is None or row.result_type != "mask_track":
        return
    payload = dict(row.result_data or {})
    payload["progress"] = progress
    row.result_data = payload
    db.commit()


def _make_keyframe(frame_idx: int, fps: float, clip_start: float, bbox: tuple[float, float, float, float], frame_size: tuple[int, int]) -> dict[str, Any]:
    """Builds a wire-shape keyframe for the frontend's `MaskKeyframe` contract:
    `t` (CLIP-relative seconds), `x`/`y`/`width`/`height` (percent-of-frame,
    from `bbox_to_transform`), and `rotation` (always 0.0 — CSRT tracks an
    axis-aligned box and never estimates rotation, so we emit it explicitly
    rather than omitting it and letting the frontend read `undefined`).
    `frame`/`time` (source-absolute) are kept alongside for debugging only;
    the frontend never reads them.
    """
    transform = bbox_to_transform(bbox, frame_size)
    source_time = frame_idx / fps if fps > 0 else 0.0
    # Clamp at zero: `anchor_frame`/`frame_idx` are rounded to whole source
    # frames, so when `clip_start` doesn't land exactly on a frame boundary
    # (e.g. clip_start=1.2133, fps=30) the conversion can come out
    # fractionally negative even though this is meant to be the clip's
    # first keyframe. `t` is a hard "clip-relative seconds, starting at 0"
    # contract other code (timeline pips, duration math, clampMasksToRange)
    # relies on without re-checking, so never emit a negative value here.
    return {
        "frame": frame_idx,
        "time": source_time,
        "t": max(0.0, source_time - clip_start),
        "rotation": 0.0,
        **transform,
    }


def _track_direction(
    cv2,
    cap,
    *,
    start_frame_idx: int,
    initial_bbox: tuple[float, float, float, float],
    frame_size: tuple[int, int],
    fps: float,
    stride: int,
    step: int,  # +1 forward, -1 backward
    total_frames: int,
    clip_start: float,
    clip_end_frame: int,
    db,
    ai_result_id: int,
) -> tuple[list[dict[str, Any]], int | None]:
    """Track from `start_frame_idx` in `step` direction, bounded at
    `clip_end_frame` going forward and at `clip_start`'s frame going
    backward (never past the source video's own bounds either). Returns
    (keyframes, lost_at_frame_or_None). Never emits keyframes past the loss
    point. Re-checks cancellation every `_CANCEL_CHECK_STRIDE` frames."""
    keyframes: list[dict[str, Any]] = []
    frame_idx = start_frame_idx
    bbox = initial_bbox
    tracker = cv2.TrackerCSRT_create()

    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
    ok, frame = cap.read()
    if not ok or frame is None:
        return keyframes, frame_idx
    tracker.init(frame, bbox)

    lower_bound = max(0, round(clip_start * fps)) if fps > 0 else 0
    upper_bound = min(total_frames - 1, clip_end_frame)

    frames_seen = 0
    while True:
        next_idx = frame_idx + step
        if next_idx < lower_bound or next_idx > upper_bound:
            break

        frames_seen += 1
        if frames_seen % _CANCEL_CHECK_STRIDE == 0 and _row_is_stopped(db, ai_result_id):
            logger.info("mask_track: ai_result %s cancelled mid-track, stopping", ai_result_id)
            break

        if step > 0:
            ok, frame = cap.read()
        else:
            cap.set(cv2.CAP_PROP_POS_FRAMES, next_idx)
            ok, frame = cap.read()
        if not ok or frame is None:
            break

        ok, tracked_box = tracker.update(frame)
        frame_idx = next_idx
        if not ok:
            return keyframes, frame_idx

        bbox = tuple(float(v) for v in tracked_box)
        if _box_left_frame(bbox, frame_size):
            return keyframes, frame_idx

        if frame_idx % stride == 0:
            keyframes.append(_make_keyframe(frame_idx, fps, clip_start, bbox, frame_size))

    return keyframes, None


def mask_track_job(ai_result_id: int) -> None:
    db = SessionLocal()
    try:
        row = db.query(AiResult).filter(AiResult.id == ai_result_id).first()
        if row is None or row.result_type != "mask_track":
            return

        if row.status not in ("queued", "processing"):
            # Already cancelled/failed before the worker picked it up.
            return

        payload = dict(row.result_data or {})
        video = db.query(Video).filter(Video.id == row.video_id).first()
        if video is None:
            raise RuntimeError("Video not found")

        row.status = "processing"
        payload["status"] = "processing"
        payload["progress"] = 0
        row.result_data = payload
        db.commit()

        mask = payload.get("mask") if isinstance(payload.get("mask"), dict) else {}
        direction = str(payload.get("direction") or "both").strip().lower()
        # `anchorTime`/`clipStart`/`clipEnd` are all CLIP-relative seconds, as
        # sent by the frontend (see MaskTrackBody in app/api/routes/ai.py).
        # The source video frame index is `(clip_start + anchor_time) * fps`.
        # Every keyframe's `t` emitted below is likewise clip-relative
        # (`source_frame / fps - clip_start`) -- this convention must not
        # drift, since the frontend's MaskKeyframe.t is defined as
        # clip-relative seconds.
        anchor_time = float(payload.get("anchorTime") or 0.0)
        clip_start = float(payload.get("clipStart") or 0.0)
        clip_end = float(payload.get("clipEnd") or 0.0)
        source_url = payload.get("sourceUrl")

        media_src = _resolve_media_source(video, source_url)
        # SSRF guard (I7): reject non-http(s) schemes and private/loopback/
        # link-local hosts before ever handing the source to OpenCV. Do not
        # echo `media_src` in any error raised from here on.
        _assert_media_source_safe(media_src)

        import cv2

        cap = cv2.VideoCapture(media_src)
        if not cap.isOpened():
            raise RuntimeError("Could not open media source for tracking.")

        try:
            frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = float(cap.get(cv2.CAP_PROP_FPS)) or 30.0
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            if frame_w <= 0 or frame_h <= 0:
                raise RuntimeError("Could not read frame dimensions from media source")
            frame_size = (frame_w, frame_h)

            # Source-absolute frame index for the clip-relative anchor time.
            anchor_frame = max(0, int(round((clip_start + anchor_time) * fps)))
            anchor_frame = min(anchor_frame, max(0, total_frames - 1))
            stride = keyframe_stride(total_frames)

            # Bound the pass at the clip's own range so tracking never wanders
            # into footage outside the keep-range the user is masking.
            clip_end_frame = (
                min(total_frames - 1, int(round(clip_end * fps)) - 1)
                if clip_end > clip_start
                else total_frames - 1
            )
            clip_end_frame = max(anchor_frame, clip_end_frame)

            initial_bbox = transform_to_bbox(mask, frame_size)
            anchor_keyframe = _make_keyframe(anchor_frame, fps, clip_start, initial_bbox, frame_size)

            lost_at: int | None = None
            keyframes: list[dict[str, Any]] = [anchor_keyframe]

            if direction in ("forward", "both"):
                fwd_keyframes, fwd_lost = _track_direction(
                    cv2,
                    cap,
                    start_frame_idx=anchor_frame,
                    initial_bbox=initial_bbox,
                    frame_size=frame_size,
                    fps=fps,
                    stride=stride,
                    step=1,
                    total_frames=total_frames,
                    clip_start=clip_start,
                    clip_end_frame=clip_end_frame,
                    db=db,
                    ai_result_id=ai_result_id,
                )
                keyframes.extend(fwd_keyframes)
                if fwd_lost is not None:
                    lost_at = fwd_lost
                _update_progress(db, ai_result_id, progress=50 if direction == "both" else 90)

            if lost_at is None and direction in ("backward", "both"):
                bwd_keyframes, bwd_lost = _track_direction(
                    cv2,
                    cap,
                    start_frame_idx=anchor_frame,
                    initial_bbox=initial_bbox,
                    frame_size=frame_size,
                    fps=fps,
                    stride=stride,
                    step=-1,
                    total_frames=total_frames,
                    clip_start=clip_start,
                    clip_end_frame=clip_end_frame,
                    db=db,
                    ai_result_id=ai_result_id,
                )
                keyframes.extend(bwd_keyframes)
                if bwd_lost is not None:
                    lost_at = bwd_lost
                _update_progress(db, ai_result_id, progress=90)
        finally:
            cap.release()

        # Final cancellation check before committing a result.
        if _row_is_stopped(db, ai_result_id):
            return

        keyframes.sort(key=lambda kf: kf["frame"])

        row = db.query(AiResult).filter(AiResult.id == ai_result_id).first()
        if row is None:
            return
        final_payload = dict(row.result_data or {})
        final_payload["keyframes"] = keyframes
        final_payload["progress"] = 100
        if lost_at is not None:
            final_payload["status"] = "partial"
            # Clip-relative seconds, matching the frontend's `formatMinSec`
            # rendering of `lostAt` -- NOT a source-absolute frame index.
            # Clamped at zero for the same rounding reason as `t` above.
            final_payload["lostAt"] = max(0.0, (lost_at / fps if fps > 0 else 0.0) - clip_start)
            row.status = "partial"
        else:
            final_payload["status"] = "completed"
            row.status = "completed"
        row.result_data = final_payload
        row.error_message = None
        db.commit()
    except Exception as exc:
        logger.exception("mask_track_job failed for ai_result=%s: %s", ai_result_id, exc)
        try:
            row = db.query(AiResult).filter(AiResult.id == ai_result_id).first()
            if row is not None:
                payload = dict(row.result_data or {})
                payload["status"] = "failed"
                payload["error"] = str(exc)
                row.status = "failed"
                row.error_message = str(exc)
                row.result_data = payload
                db.commit()
        except Exception:
            logger.exception("mask_track_job: failed to record failure for ai_result=%s", ai_result_id)
    finally:
        db.close()
