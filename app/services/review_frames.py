"""Frame sampling for the multimodal AI review.

The AI review used to read only the transcript, so it could talk about pacing
and filler words but never about what the video *looks* like. This module picks
a handful of representative timestamps, extracts them with ffmpeg, and uploads
them so the review prompt can include real frames and each suggestion can show
the frame it refers to.

Everything here is best-effort: a failure returns fewer frames (or none) and the
review falls back to transcript-only. See docs/player-ai-review-polish-plan.md.
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from app.services.thumbnail import generate_thumbnail_to_path
from app.storage import build_key, get_storage

logger = logging.getLogger(__name__)

DEFAULT_FRAME_COUNT = 10
_MAX_FRAME_COUNT = 24
#: Two candidates closer than this collapse into one — near-identical frames
#: cost tokens without telling the model anything new.
_MIN_FRAME_GAP_SEC = 1.0
#: Viewers decide in the first few seconds, so the hook gets its own samples.
_HOOK_WINDOW_SEC = 15.0
_EDGE_MARGIN_SEC = 0.2

# Candidate priorities — lower wins when two candidates collide, and when the
# list has to be trimmed to the frame budget.
_P_HOOK = 0
_P_BODY = 1
_P_ISSUE = 2

#: Suggestion kinds worth looking at: a bad take or a repeated line usually has
#: a visible tell (glance off-camera, reset posture). Silences do not.
_ISSUE_KINDS = {"bad_take", "repeat"}


def frame_budget() -> int:
    """How many frames to sample, from ``AI_REVIEW_FRAME_COUNT``."""
    raw = (os.getenv("AI_REVIEW_FRAME_COUNT") or "").strip()
    try:
        value = int(raw) if raw else DEFAULT_FRAME_COUNT
    except ValueError:
        value = DEFAULT_FRAME_COUNT
    return max(1, min(_MAX_FRAME_COUNT, value))


def _hook_candidates(duration: float) -> list[float]:
    """Up to three samples inside the hook window, scaled to short videos."""
    window = min(_HOOK_WINDOW_SEC, duration)
    if window <= 0:
        return []
    return [window * fraction for fraction in (0.04, 0.35, 0.85)]


def _body_candidates(duration: float, count: int) -> list[float]:
    """Evenly spaced samples across the whole video, excluding the very edges."""
    if count <= 0 or duration <= 0:
        return []
    # (i + 1) / (count + 1) keeps every sample strictly inside the video.
    return [duration * (index + 1) / (count + 1) for index in range(count)]


def _issue_candidates(analysis: dict[str, Any] | None) -> list[float]:
    if not isinstance(analysis, dict):
        return []
    out: list[float] = []
    for suggestion in analysis.get("suggestions") or []:
        if not isinstance(suggestion, dict):
            continue
        if suggestion.get("kind") not in _ISSUE_KINDS:
            continue
        try:
            start = float(suggestion.get("start") or 0.0)
        except (TypeError, ValueError):
            continue
        # A beat into the segment, so the frame shows the take rather than the
        # cut into it.
        out.append(start + 0.3)
    return out


def pick_timestamps(
    duration: float,
    analysis: dict[str, Any] | None = None,
    *,
    limit: int | None = None,
) -> list[float]:
    """Choose which seconds of the video to screenshot.

    Hook samples come first (they always survive trimming), then an even spread
    across the body, then any detected bad takes / repeated lines. Returns a
    sorted, de-duplicated list clamped inside the video.
    """
    try:
        duration = float(duration or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    budget = limit if limit is not None else frame_budget()
    budget = max(1, min(_MAX_FRAME_COUNT, budget))
    if duration <= 0:
        return []

    hook = [(t, _P_HOOK) for t in _hook_candidates(duration)]
    # The hook already covers the opening, so the body spread fills the rest of
    # the budget with room left for a couple of issue anchors.
    body_count = max(0, budget - len(hook) - 2)
    body = [(t, _P_BODY) for t in _body_candidates(duration, body_count)]
    issues = [(t, _P_ISSUE) for t in _issue_candidates(analysis)]

    lo = min(_EDGE_MARGIN_SEC, duration / 2)
    hi = max(lo, duration - _EDGE_MARGIN_SEC)
    clamped = [(min(max(t, lo), hi), priority) for t, priority in hook + body + issues]

    # Collapse near-identical timestamps, keeping the highest-priority one.
    kept: list[tuple[float, int]] = []
    for timestamp, priority in sorted(clamped, key=lambda pair: (pair[1], pair[0])):
        if any(abs(timestamp - other) < _MIN_FRAME_GAP_SEC for other, _ in kept):
            continue
        kept.append((timestamp, priority))

    kept.sort(key=lambda pair: (pair[1], pair[0]))
    return sorted(round(timestamp, 2) for timestamp, _ in kept[:budget])


def extract_frames(src: str, timestamps: list[float], dst_dir: Path) -> list[tuple[float, Path]]:
    """Extract one JPEG per timestamp into ``dst_dir``.

    Timestamps ffmpeg can't reach are skipped rather than raising — a partial
    filmstrip is still useful to the model.
    """
    if not src:
        return []
    frames: list[tuple[float, Path]] = []
    for index, timestamp in enumerate(timestamps):
        dst = dst_dir / f"frame_{index:02d}.jpg"
        if generate_thumbnail_to_path(src, dst, seek=timestamp):
            frames.append((timestamp, dst))
        else:
            logger.warning("review frame extraction failed at %.2fs for %s", timestamp, src)
    return frames


def extract_and_store_frames(
    video_id: int, src: str, timestamps: list[float]
) -> tuple[list[dict[str, Any]], list[bytes]]:
    """Extract frames, upload them, and hand back both the stored descriptors
    (``[{"t": float, "url": str}, ...]``, what the UI renders) and the JPEG
    bytes in the same order (what the model is shown) — so the caller never has
    to download what it just uploaded.

    Never raises: on any failure the review continues with fewer frames, or
    none at all.
    """
    if not src or not timestamps:
        return [], []
    stored: list[dict[str, Any]] = []
    blobs: list[bytes] = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for timestamp, path in extract_frames(src, timestamps, Path(tmp)):
                try:
                    blob = path.read_bytes()
                except OSError:
                    logger.warning("could not read review frame %s", path)
                    continue
                key = build_key(
                    folder="ai_review_frames",
                    public_id=f"{video_id}/{path.stem}",
                    content_type="image/jpeg",
                )
                try:
                    result = get_storage().upload_path(
                        path, key=key, content_type="image/jpeg"
                    )
                except Exception:
                    logger.exception("review frame upload failed for video %s", video_id)
                    continue
                url = getattr(result, "url", None)
                if url:
                    stored.append({"t": round(timestamp, 2), "url": url})
                    blobs.append(blob)
    except Exception:
        logger.exception("review frame extraction aborted for video %s", video_id)
    return stored, blobs
