"""Server-side rough-cut auto-edit analysis.

`_analyze_segments` (and its supporting constants) used to live in
`app/api/routes/ai.py`. It is moved here so `app/jobs/transcription.py` (an
RQ worker module) can call it directly after a transcription completes,
without importing the routes module and its FastAPI/router/AI-client
dependencies. `app/api/routes/ai.py` imports the public names back from this
module, so existing routes/tests that reference
`app.api.routes.ai._analyze_segments` / `.AutoEditOptions` / `._summarize_analysis`
keep working unchanged.

This module also owns:
- `run_post_transcription_auto_edit`: the transcription-completion hook that
  runs the analysis (only when the video has enabled auto-edit prefs) and
  seeds/merges the result into the `rough_cut_draft` AiResult. Never raises —
  analysis failures must not fail the transcription job.
- `filter_segments_to_ranges`: a pure helper used by `repurpose_pipeline` to
  keep clip-suggestion windows out of ranges the auto-edit (or the user) cut.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_FILLERS = {"um", "umm", "uh", "uhh", "ah", "erm"}
_BAD_TAKE_RE = re.compile(
    r"\b(cut that|bad take|start over|restart|retake|scratch that|messed up|ignore that|redo that|try again)\b",
    re.IGNORECASE,
)
_SILENCE_THRESHOLD = 0.65

Aggressiveness = Literal["light", "balanced", "aggressive"]

# Silence-gap threshold (seconds) by aggressiveness. "balanced" preserves the
# original hardcoded _SILENCE_THRESHOLD for back-compat.
_SILENCE_THRESHOLD_BY_AGGRESSIVENESS: dict[str, float] = {
    "light": 0.9,
    "balanced": _SILENCE_THRESHOLD,
    "aggressive": 0.4,
}


def _normalize_token(word: str) -> str:
    """Lowercase, punctuation-free form used for repetition comparison."""
    return re.sub(r"[^\w']", "", word).lower()


def _segment_word_times(seg: dict) -> list[tuple[str, float, float]] | None:
    """Per-word (text, start, end) for a segment, or None if untimed.

    Prefers real Whisper word timestamps (`app.jobs.transcription` persists
    `words: [{word,start,end}]` per segment when available); falls back to an
    even division of the segment. Repetition cuts are sub-word-accurate edits,
    so a bad estimate is worse than none — the fallback is only trusted because
    it is the same one the filler pass has always used.
    """
    text = str(seg.get("text") or "")
    tokens = text.split()
    if not tokens:
        return None
    start = float(seg.get("start") or 0)
    end = float(seg.get("end") or start)
    if end <= start:
        return None

    seg_words = seg.get("words")
    if isinstance(seg_words, list) and len(seg_words) == len(tokens):
        timed: list[tuple[str, float, float]] = []
        for token, raw in zip(tokens, seg_words):
            try:
                timed.append((token, float(raw.get("start")), float(raw.get("end"))))
            except (TypeError, ValueError, AttributeError):
                timed = []
                break
        if timed:
            return timed

    span = (end - start) / len(tokens)
    return [(token, start + i * span, start + (i + 1) * span) for i, token in enumerate(tokens)]


def _detect_repeats_in_segment(seg: dict, index: int) -> list[dict]:
    """Consecutive repeated words and phrases inside one segment.

    A speaker restarting a phrase ("I want to — I want to build this") leaves
    the same tokens twice in a row. The *last* utterance is the one that leads
    into the surviving sentence, so every repetition before it is proposed for
    removal. Phrases are scanned longest-first, which stops "we should we
    should" from being reported as two separate word repeats.
    """
    timed = _segment_word_times(seg)
    if not timed or len(timed) < 2:
        return []

    tokens = [_normalize_token(word) for word, _, _ in timed]
    suggestions: list[dict] = []
    taken: set[int] = set()

    # Phrases (longest first), then single words for whatever is left.
    for size in range(min(6, len(tokens) // 2), 0, -1):
        position = 0
        while position + size * 2 <= len(tokens):
            window = range(position, position + size * 2)
            first = tokens[position : position + size]
            second = tokens[position + size : position + size * 2]
            if any(t in taken for t in window) or not all(first) or first != second:
                position += 1
                continue

            start = timed[position][1]
            end = timed[position + size - 1][2]
            if end > start:
                suggestions.append({
                    "id": f"repeat-{index}-{position}-{size}",
                    "kind": "repeat",
                    "title": "Repeated phrase" if size > 1 else "Repeated word",
                    "detail": " ".join(word for word, _, _ in timed[position : position + size]),
                    "start": start,
                    "end": end,
                    "severity": "medium" if size == 1 else "high",
                })
                taken.update(window)
            position += size * 2

    return suggestions


def _duplicate_segment_indices(segments: list[dict]) -> dict[int, str]:
    """Indices of segments whose text repeats the segment right before them.

    Maps index → the duplicated text. Only the *earlier* copy is flagged: when
    a speaker delivers the same sentence twice, the later take is the one they
    meant to keep.
    """
    duplicates: dict[int, str] = {}
    previous_key: str | None = None
    previous_index: int | None = None
    for i, seg in enumerate(segments):
        text = str(seg.get("text") or "").strip()
        key = " ".join(_normalize_token(w) for w in text.split()).strip()
        # Single words and very short interjections repeat legitimately.
        if key and len(key.split()) >= 3 and key == previous_key and previous_index is not None:
            duplicates[previous_index] = text
        previous_key = key or None
        previous_index = i
    return duplicates


def _analyze_segments(
    segments: list[dict],
    duration: float,
    *,
    remove_fillers: bool = True,
    remove_silences: bool = True,
    remove_bad_takes: bool = True,
    remove_repeats: bool = True,
    aggressiveness: Aggressiveness = "balanced",
) -> dict:
    """Server-side rough-cut analysis: derive keep_ranges and suggestion list from transcript.

    remove_fillers/remove_silences/remove_bad_takes gate whether each category
    contributes suggestions at all; a disabled category never reduces
    keepRanges either (its segments/gaps are treated as ordinary kept content).

    aggressiveness only has a clean, non-invented knob for silence (the gap
    threshold below). Filler and bad-take detection are word-set / regex based
    with no existing continuous strictness dimension, so they are intentionally
    left unscaled by aggressiveness — see task report.
    """
    silence_threshold = _SILENCE_THRESHOLD_BY_AGGRESSIVENESS.get(aggressiveness, _SILENCE_THRESHOLD)
    keep: list[dict] = []
    suggestions: list[dict] = []

    # A sentence delivered twice in a row: the earlier copy leaves keep_ranges
    # entirely, the way a bad take does.
    duplicate_segments = _duplicate_segment_indices(segments) if remove_repeats else {}

    for i, seg in enumerate(segments):
        start = float(seg.get("start") or 0)
        end = float(seg.get("end") or start)
        text = str(seg.get("text") or "").strip()
        if end <= start:
            continue

        # Bad-take detection — mark as suggestion, exclude from keep (only when enabled)
        if remove_bad_takes and _BAD_TAKE_RE.search(text):
            suggestions.append({
                "id": f"bad-take-{i}",
                "kind": "bad_take",
                "title": "Bad take",
                "detail": text[:90],
                "start": start,
                "end": end,
                "severity": "high",
            })
            continue

        if i in duplicate_segments:
            suggestions.append({
                "id": f"repeat-segment-{i}",
                "kind": "repeat",
                "title": "Repeated sentence",
                "detail": duplicate_segments[i][:90],
                "start": start,
                "end": end,
                "severity": "high",
            })
            continue

        # Silence gap before this segment
        if remove_silences and i > 0:
            prev_end = float(segments[i - 1].get("end") or 0)
            gap = start - prev_end
            if gap >= silence_threshold:
                gap_start = prev_end + 0.08
                gap_end = start - 0.08
                if gap_end > gap_start:
                    suggestions.append({
                        "id": f"silence-{i}",
                        "kind": "silence",
                        "title": "Silence",
                        "detail": f"{gap:.1f}s",
                        "start": gap_start,
                        "end": gap_end,
                        "severity": "high" if gap > 1.3 else "medium",
                    })

        keep.append({"start": start, "end": end})

    # Merge adjacent keep ranges
    merged: list[dict] = []
    for rng in sorted(keep, key=lambda r: r["start"]):
        if merged and rng["start"] <= merged[-1]["end"] + 0.03:
            merged[-1]["end"] = max(merged[-1]["end"], rng["end"])
        else:
            merged.append(dict(rng))

    # Filler suggestions (word-level — coarse segment scan)
    if remove_fillers:
        for i, seg in enumerate(segments):
            words = str(seg.get("text") or "").lower().split()
            start = float(seg.get("start") or 0)
            end = float(seg.get("end") or start)
            span = max(end - start, 0.2) / max(len(words), 1)

            # Prefer real Whisper word timestamps (app.jobs.transcription
            # persists `words: [{word,start,end}]` per segment when available)
            # over the even-division estimate below. Only trusted when the
            # word count lines up with the whitespace-split token count used
            # for indexing below — otherwise fall back cleanly.
            word_times: list[tuple[float, float]] | None = None
            seg_words = seg.get("words")
            if isinstance(seg_words, list) and len(seg_words) == len(words):
                candidate: list[tuple[float, float]] = []
                for w in seg_words:
                    try:
                        candidate.append((float(w.get("start")), float(w.get("end"))))
                    except (TypeError, ValueError, AttributeError):
                        candidate = []
                        break
                if candidate:
                    word_times = candidate

            for j, w in enumerate(words):
                clean = re.sub(r"[^\w']", "", w)
                if clean in _FILLERS:
                    if word_times is not None:
                        ws, we = word_times[j]
                    else:
                        ws = start + j * span
                        we = ws + span
                    suggestions.append({
                        "id": f"filler-{i}-{j}",
                        "kind": "filler",
                        "title": "Filler",
                        "detail": w,
                        "start": max(0, ws - 0.03),
                        "end": min(duration, we + 0.03),
                        "severity": "high" if clean in {"um", "uh"} else "medium",
                    })

    # Word/phrase repeats inside surviving segments. A segment already dropped
    # whole (bad take, duplicate sentence) has nothing left to deduplicate.
    if remove_repeats:
        for i, seg in enumerate(segments):
            if i in duplicate_segments:
                continue
            if remove_bad_takes and _BAD_TAKE_RE.search(str(seg.get("text") or "")):
                continue
            suggestions.extend(_detect_repeats_in_segment(seg, i))

    suggestions.sort(key=lambda s: (s["start"], s["end"]))
    return {"keepRanges": merged, "suggestions": suggestions}


class AutoEditOptions(BaseModel):
    """Category flags + aggressiveness for the rough-cut auto-edit detector.

    Defaults match _analyze_segments' historical (pre-B3) behavior, so an
    absent/empty body is fully back-compat.
    """

    remove_fillers: bool = True
    remove_silences: bool = True
    remove_bad_takes: bool = True
    remove_repeats: bool = True
    aggressiveness: Aggressiveness = "balanced"


_REVIEW_KIND_MAP = {
    "filler": "fillers",
    "silence": "silences",
    "bad_take": "bad_takes",
    "repeat": "repeats",
}


def _summarize_analysis(analysis: dict) -> tuple[dict, float]:
    """Group suggestions into {fillers,silences,bad_takes,repeats} counts + removable seconds."""
    counts = {"fillers": 0, "silences": 0, "bad_takes": 0, "repeats": 0}
    removable = 0.0
    for s in analysis.get("suggestions", []):
        key = _REVIEW_KIND_MAP.get(s.get("kind"))
        if not key:
            continue
        counts[key] += 1
        removable += max(float(s.get("end", 0)) - float(s.get("start", 0)), 0.0)
    return counts, removable


def filter_segments_to_ranges(
    segments: list[dict[str, Any]], ranges: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Intersect transcript segments against kept time ranges (e.g. a rough-cut
    draft's `keepRanges`), so downstream clip suggestion never proposes a
    window inside time the auto-edit (or the user) removed.

    Other segment keys (text, speaker, ...) are preserved; only start/end are
    clipped to the overlap. A segment that straddles two disjoint kept ranges
    (rare — keep ranges are themselves built from segment boundaries) yields
    one output entry per overlapping range.
    """
    if not ranges:
        return list(segments)

    sorted_ranges: list[dict[str, float]] = []
    for r in ranges:
        try:
            start = float(r.get("start", 0.0))
            end = float(r.get("end", start))
        except (TypeError, ValueError, AttributeError):
            continue
        if end > start:
            sorted_ranges.append({"start": start, "end": end})
    sorted_ranges.sort(key=lambda r: r["start"])
    if not sorted_ranges:
        return list(segments)

    out: list[dict[str, Any]] = []
    for seg in segments:
        try:
            s = float(seg.get("start", 0.0))
            e = float(seg.get("end", s))
        except (TypeError, ValueError, AttributeError):
            continue
        if e <= s:
            continue
        for rng in sorted_ranges:
            overlap_start = max(s, rng["start"])
            overlap_end = min(e, rng["end"])
            if overlap_end > overlap_start:
                clipped = dict(seg)
                clipped["start"] = overlap_start
                clipped["end"] = overlap_end
                out.append(clipped)
    return out


def run_post_transcription_auto_edit(
    db: Session,
    video_id: int,
    *,
    segments: list[dict[str, Any]],
    video_duration: float | None,
    transcription_id: int | None,
) -> None:
    """Post-transcription hook: if the video has enabled auto-edit prefs, run
    `_analyze_segments` and seed/merge the result into the video's
    `rough_cut_draft` AiResult.

    - `keepRanges` is only written when `auto_apply` is true AND there is no
      existing user draft (a draft with a `rangeEditVersion` key — the
      frontend rough-cut editor stamps every save with that key, so its
      presence means a human has already opened/edited the draft and must
      never be clobbered by a background job).
    - `aiAnalysis` (`{suggestions, counts, options, analyzedAt}`) is always
      written when analysis runs, independent of `auto_apply`, so the Edit
      tab can show suggestions even when auto-apply is off.
    - `analyzedAt` is a transcription-id-based marker (not a wall-clock
      timestamp) so re-running this hook for the same transcription is
      deterministic/comparable in tests.
    - Never raises: any failure is logged and swallowed so a broken analysis
      can never fail the transcription job itself.
    """
    from app.db.models import AiResult

    try:
        prefs_row = (
            db.query(AiResult)
            .filter(AiResult.video_id == video_id, AiResult.result_type == "auto_edit_prefs")
            .first()
        )
        if not prefs_row or not isinstance(prefs_row.result_data, dict):
            return
        prefs_data = prefs_row.result_data
        if not prefs_data.get("enabled"):
            return

        options = AutoEditOptions(
            remove_fillers=prefs_data.get("remove_fillers", True),
            remove_silences=prefs_data.get("remove_silences", True),
            remove_bad_takes=prefs_data.get("remove_bad_takes", True),
            remove_repeats=prefs_data.get("remove_repeats", True),
            aggressiveness=prefs_data.get("aggressiveness", "balanced"),
        )
        auto_apply = bool(prefs_data.get("auto_apply"))

        duration = float(video_duration or 0)
        range_start = prefs_data.get("source_range_start_seconds")
        range_end = prefs_data.get("source_range_end_seconds")
        source_ranges = []
        if range_start is not None and range_end is not None and float(range_end) > float(range_start):
            source_ranges = [{"start": float(range_start), "end": float(range_end)}]
        scoped_segments = filter_segments_to_ranges(segments, source_ranges) if source_ranges else segments
        analysis = _analyze_segments(
            scoped_segments,
            duration,
            remove_fillers=options.remove_fillers,
            remove_silences=options.remove_silences,
            remove_bad_takes=options.remove_bad_takes,
            remove_repeats=options.remove_repeats,
            aggressiveness=options.aggressiveness,
        )
        if source_ranges:
            scoped_keep_ranges: list[dict[str, float]] = []
            for keep_range in analysis.get("keepRanges", []):
                start = max(float(keep_range.get("start", 0)), source_ranges[0]["start"])
                end = min(float(keep_range.get("end", 0)), source_ranges[0]["end"])
                if end > start:
                    scoped_keep_ranges.append({"start": start, "end": end})
            analysis["keepRanges"] = scoped_keep_ranges
        counts, _removable = _summarize_analysis(analysis)

        draft_row = (
            db.query(AiResult)
            .filter(AiResult.video_id == video_id, AiResult.result_type == "rough_cut_draft")
            .first()
        )
        existing_data = (
            dict(draft_row.result_data) if draft_row and isinstance(draft_row.result_data, dict) else {}
        )
        has_user_range_edits = "rangeEditVersion" in existing_data

        new_data = dict(existing_data)
        new_data["aiAnalysis"] = {
            "suggestions": analysis.get("suggestions", []),
            "counts": counts,
            "options": options.model_dump(mode="json"),
            "analyzedAt": f"transcription:{transcription_id}",
        }
        if auto_apply and not has_user_range_edits:
            new_data["keepRanges"] = analysis.get("keepRanges", [])

        if draft_row is None:
            draft_row = AiResult(video_id=video_id, result_type="rough_cut_draft")
            db.add(draft_row)
        draft_row.status = "completed"
        draft_row.error_message = None
        draft_row.result_data = new_data
        db.commit()
        logger.info(
            "Post-transcription auto-edit seeded rough_cut_draft for video %s (auto_apply=%s, applied_keep_ranges=%s)",
            video_id,
            auto_apply,
            auto_apply and not has_user_range_edits,
        )
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.exception("Post-transcription auto-edit analysis failed for video %s", video_id)
