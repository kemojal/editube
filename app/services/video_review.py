"""Pro-tier AI video review.

Builds the ``review`` AiResult payload the player's AI panel renders: an
engagement score, per-dimension sub-scores, strengths, and timestamped notes
that each say what's wrong, why it costs the viewer, and how to fix it.

Unlike the old transcript-only review, the model also sees real frames sampled
by ``app.services.review_frames``, so it can speak to framing, lighting,
on-screen text and energy. Frames are optional: everything degrades to a
transcript-only review, and then to a heuristic score without a Gemini key.
"""
from __future__ import annotations

import logging
from typing import Any

from app.services.ai_client import generate_json_multimodal
from app.services.auto_edit import AutoEditOptions, _analyze_segments, _summarize_analysis
from app.services.review_frames import extract_and_store_frames, pick_timestamps

logger = logging.getLogger(__name__)

#: Dimensions scored 0-100 alongside the overall engagement score. Order is the
#: order the player renders them in.
SCORE_DIMENSIONS = ("hook", "pacing", "clarity", "visuals", "audio", "structure")
#: Buckets a note can be filed under; anything else is normalized to "clarity".
NOTE_CATEGORIES = set(SCORE_DIMENSIONS)

_MAX_TRANSCRIPT_SEGMENTS = 400
_MAX_COMMENTS = 60
_MAX_NOTES = 8
_MAX_STRENGTHS = 4
_MAX_THUMBNAIL_MOMENTS = 3

_PROMPT = """You are a senior YouTube editor reviewing one video. You have the \
transcript, the team's review comments, automatic filler/silence/bad-take \
detection, and screenshots sampled across the video (timestamps listed below, \
in the same order as the attached images).

Judge it the way a viewer decides whether to keep watching: hook, pacing, \
clarity, visuals, audio, structure.

Write like a note passed to the editor, not an essay. Hard limits, obey them:
- verdict: ONE sentence, max 16 words.
- each improvement `text`: max 8 words, names the problem.
- each improvement `fix`: max 14 words, an instruction the editor can act on.
- each strength: max 8 words.
- No preamble, no restating the question, no praise padding, no hedging.
- Only report things that change the cut. Skip anything a viewer wouldn't notice.

Return JSON:
{"engagement_score": number 0-100,
 "verdict": string,
 "scores": {"hook": number, "pacing": number, "clarity": number, \
"visuals": number, "audio": number, "structure": number},
 "strengths": [string],
 "improvements": [{"text": string, "fix": string, "start": seconds, \
"end": seconds or null, "severity": "low"|"medium"|"high", \
"category": one of hook|pacing|clarity|visuals|audio|structure}],
 "thumbnail_moments": [{"t": seconds, "reason": string max 8 words}]}

"""


def empty_review(message: str) -> dict[str, Any]:
    """The payload shown when there's nothing to review yet."""
    return {
        "needs_transcription": True,
        "engagement_score": None,
        "verdict": message,
        "scores": {},
        "strengths": [],
        "improvements": [],
        "frames": [],
        "thumbnail_moments": [],
        "counts": {"fillers": 0, "silences": 0, "bad_takes": 0},
        "removable_seconds": 0,
        "keepRanges": [],
    }


def heuristic_score(counts: dict[str, Any], duration: float) -> int:
    """Filler/silence/bad-take penalty score. Also the no-API-key fallback."""
    minutes = max(float(duration or 0) / 60.0, 0.5)
    filler_rate = float(counts.get("fillers", 0)) / minutes
    penalty = (
        min(filler_rate * 4, 30)
        + min(float(counts.get("silences", 0)) * 2, 20)
        + min(float(counts.get("bad_takes", 0)) * 5, 25)
    )
    return int(max(35, round(100 - penalty)))


def _clamp_score(value: Any, default: int) -> int:
    try:
        score = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(0, min(100, score))


def _as_seconds(value: Any) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def _nearest_frame_url(frames: list[dict[str, Any]], start: float | None) -> str | None:
    """The sampled frame closest to a note's timestamp, so the note can show
    what the reviewer was looking at."""
    if start is None or not frames:
        return None
    best = min(frames, key=lambda frame: abs(float(frame.get("t", 0)) - start))
    return best.get("url")


def _harden_scores(raw: Any, overall: int) -> dict[str, int]:
    """Bare number per dimension. The number is the whole signal — a sentence
    explaining each one is what the notes are for."""
    source = raw if isinstance(raw, dict) else {}
    scores: dict[str, int] = {}
    for dimension in SCORE_DIMENSIONS:
        entry = source.get(dimension)
        # Tolerate the older {score, note} shape still sitting in stored rows.
        value = entry.get("score") if isinstance(entry, dict) else entry
        scores[dimension] = _clamp_score(value, overall)
    return scores


def _harden_notes(raw: Any, frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    notes: list[dict[str, Any]] = []
    for item in (raw or [])[:_MAX_NOTES]:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        start = _as_seconds(item.get("start"))
        end = _as_seconds(item.get("end"))
        if end is not None and start is not None and end <= start:
            end = None
        severity = str(item.get("severity") or "medium").lower()
        if severity not in {"low", "medium", "high"}:
            severity = "medium"
        category = str(item.get("category") or "").lower()
        if category not in NOTE_CATEGORIES:
            category = "clarity"
        notes.append(
            {
                "text": text,
                "fix": str(item.get("fix") or "").strip(),
                "start": start,
                "end": end,
                "severity": severity,
                "category": category,
                "frame_url": _nearest_frame_url(frames, start),
            }
        )
    return notes


def _harden_thumbnail_moments(
    raw: Any, frames: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    moments: list[dict[str, Any]] = []
    for item in (raw or [])[:_MAX_THUMBNAIL_MOMENTS]:
        if not isinstance(item, dict):
            continue
        timestamp = _as_seconds(item.get("t"))
        if timestamp is None:
            continue
        moments.append(
            {
                "t": round(timestamp, 2),
                "reason": str(item.get("reason") or "").strip(),
                "url": _nearest_frame_url(frames, timestamp),
            }
        )
    return moments


def harden_review(
    raw: Any,
    *,
    fallback_score: int,
    frames: list[dict[str, Any]],
    counts: dict[str, Any],
    removable_seconds: float,
    keep_ranges: list[dict[str, Any]],
) -> dict[str, Any]:
    """Force a model response into the stable shape the player renders.

    The panel must never crash on a malformed field, so every value is coerced
    and every list is bounded.
    """
    review = raw if isinstance(raw, dict) else {}
    overall = _clamp_score(review.get("engagement_score"), fallback_score)
    strengths = [
        str(item).strip()
        for item in (review.get("strengths") or [])
        if str(item).strip()
    ][:_MAX_STRENGTHS]

    return {
        "needs_transcription": False,
        "engagement_score": overall,
        "verdict": str(review.get("verdict") or "").strip(),
        "scores": _harden_scores(review.get("scores"), overall),
        "strengths": strengths,
        "improvements": _harden_notes(review.get("improvements"), frames),
        "thumbnail_moments": _harden_thumbnail_moments(
            review.get("thumbnail_moments"), frames
        ),
        "frames": frames,
        "counts": dict(counts),
        "removable_seconds": round(float(removable_seconds), 1),
        "keepRanges": keep_ranges,
    }


def build_review(
    *,
    video_id: int,
    duration: float,
    media_src: str,
    segments: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    options: AutoEditOptions | None = None,
) -> dict[str, Any]:
    """Run the whole review: analyze → sample frames → ask the model → harden.

    ``segments`` empty means there's nothing to review yet; the caller should
    have checked, but this guards the job path too.
    """
    if not segments:
        return empty_review("Transcribe the video first to run an AI review.")

    opts = options or AutoEditOptions()
    analysis = _analyze_segments(
        segments,
        duration,
        remove_fillers=opts.remove_fillers,
        remove_silences=opts.remove_silences,
        remove_bad_takes=opts.remove_bad_takes,
        remove_repeats=opts.remove_repeats,
        aggressiveness=opts.aggressiveness,
    )
    counts, removable = _summarize_analysis(analysis)
    baseline = heuristic_score(counts, duration)

    timestamps = pick_timestamps(duration, analysis)
    frames, frame_blobs = extract_and_store_frames(video_id, media_src, timestamps)

    fallback = {
        "engagement_score": baseline,
        "verdict": "Heuristic score only — set GEMINI_API_KEY for a full review.",
    }

    context = {
        "duration_sec": round(float(duration or 0), 1),
        "detected": counts,
        "frames": [frame["t"] for frame in frames],
        "transcript": segments[:_MAX_TRANSCRIPT_SEGMENTS],
        "team_comments": comments[:_MAX_COMMENTS],
    }

    try:
        raw = generate_json_multimodal(
            f"{_PROMPT}{context}",
            frame_blobs,
            fallback=fallback,
        )
    except Exception as exc:  # noqa: BLE001 — a model failure must not 500 the job
        logger.warning("AI review model call failed for video %s: %s", video_id, exc)
        raw = fallback

    return harden_review(
        raw,
        fallback_score=baseline,
        frames=frames,
        counts=counts,
        removable_seconds=removable,
        keep_ranges=analysis.get("keepRanges", []),
    )
