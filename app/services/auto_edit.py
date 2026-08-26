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
from difflib import SequenceMatcher
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.services.word_alignment import timed_words_for_tokens

logger = logging.getLogger(__name__)

# Hesitation sounds — cut whenever they stand alone as a token. Mirrors the
# frontend's transcript-cleanup list so server counts and on-screen proposals
# agree.
_STRONG_FILLERS = {
    "um", "umm", "ummm", "uh", "uhh", "uhhh", "uhm", "ah", "ahh",
    "er", "err", "erm", "ehm", "hmm", "hm", "mhm", "mm", "mmm",
}
# Discourse words that are only fillers when detached from the sentence —
# fronted or trailed by a real pause. "like" inside "I like this" must survive.
_WEAK_FILLERS = {
    "like", "so", "well", "right", "okay", "ok", "yeah", "actually",
    "basically", "literally", "honestly", "anyway",
}
# Two-word discourse fillers, matched on normalized token pairs.
_FILLER_BIGRAMS = {
    ("you", "know"), ("i", "mean"), ("kind", "of"), ("sort", "of"),
}
# Back-compat export: older tests/imports read `_FILLERS` as "the filler set".
_FILLERS = _STRONG_FILLERS | _WEAK_FILLERS

# Gap (seconds) on either side that makes a weak filler count as detached.
_WEAK_DETACH_GAP = 0.18

_BAD_TAKE_RE = re.compile(
    r"\b(cut that|bad take|start over|restart|retake|scratch that|messed up|"
    r"ignore that|redo that|try again|let me (?:try|do|say) that again|"
    r"let's (?:try|do) that again|i'?ll (?:say|do) that again|from the top)\b",
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

# Head/tail of a detected gap left in place so the cut keeps natural rhythm
# (and never clips a word onset). Segment/word gap edges get the historical
# 0.08s; VAD silences are already padded by speech_pad_ms at detection time,
# so they keep a smaller inset.
_SILENCE_EDGE_KEEP = 0.08
_VAD_SILENCE_EDGE_KEEP = 0.04
# A silence cut shorter than this after insets isn't worth a blade mark.
_MIN_SILENCE_CUT = 0.15
# Silence severity boundary (seconds of raw gap).
_SILENCE_HIGH_SEVERITY = 1.3
# Keep-range fragments shorter than this after silence subtraction are noise.
_MIN_KEEP_FRAGMENT = 0.12
# Near-duplicate consecutive sentences at or above this similarity are retakes.
_RETAKE_SIMILARITY = 0.8


def _normalize_token(word: str) -> str:
    """Lowercase, punctuation-free form used for repetition comparison."""
    return re.sub(r"[^\w']", "", word).lower()


def _segment_word_times(seg: dict) -> list[tuple[str, float, float]] | None:
    """Per-word (text, start, end) for a segment, or None if untimed.

    Real Whisper word timestamps are fuzzily aligned to the segment's tokens
    (`app.services.word_alignment`), so a tokenization mismatch — Whisper
    splitting "cross-platform." into two words — no longer discards the whole
    segment's timings and erases its internal pauses. Tokens without a real
    counterpart are interpolated between their timed neighbours; with no
    usable words at all this degrades to the historical even division.
    """
    text = str(seg.get("text") or "")
    tokens = text.split()
    if not tokens:
        return None
    start = float(seg.get("start") or 0)
    end = float(seg.get("end") or start)
    if end <= start:
        return None

    try:
        return timed_words_for_tokens(tokens, seg.get("words"), seg_start=start, seg_end=end)
    except Exception:  # never let alignment take down analysis
        logger.exception("Word alignment failed for segment at %.2fs", start)
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

            # Deliberate rhetorical chaining ("Windows to Mac, Mac to iPhone")
            # repeats a word on purpose: the first copy carries clause
            # punctuation and a spoken beat separates the pair. A stutter has
            # neither. Only single-word repeats can be chains — an exact
            # multi-word adjacent duplicate is a restart.
            if size == 1:
                first_raw = timed[position][0].rstrip()
                pause = timed[position + 1][1] - timed[position][2]
                if first_raw[-1:] in {",", ";", ":"} or pause >= 0.15:
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

    # Cut-off words ("I wen- I went…"): Whisper writes the abandoned attempt
    # with a trailing dash. The restart itself is caught by the repeat scan
    # above when it echoes; the dangling fragment never echoes, so catch it
    # here by shape.
    for position, (word, start, end) in enumerate(timed):
        if position in taken:
            continue
        stripped = word.rstrip(".,!?")
        if len(stripped) >= 2 and stripped[-1] in "-–—" and stripped[:-1].isalpha():
            if end > start:
                suggestions.append({
                    "id": f"false-start-{index}-{position}",
                    "kind": "repeat",
                    "title": "False start",
                    "detail": word,
                    "start": start,
                    "end": end,
                    "severity": "medium",
                })
                taken.add(position)

    return suggestions


def _duplicate_segment_indices(segments: list[dict]) -> dict[int, str]:
    """Indices of segments that a following segment re-delivers (retakes).

    Maps index → the duplicated text. Only the *earlier* copy is flagged: when
    a speaker delivers the same sentence twice, the later take is the one they
    meant to keep. Three shapes count:

    - exact repeat (normalized token equality),
    - near-duplicate (SequenceMatcher ≥ _RETAKE_SIMILARITY — a retake is
      rarely word-for-word identical),
    - abandoned start: the earlier segment is a strict prefix of the later
      one ("Welcome to my channel" → "Welcome to my channel, where we…").
    """
    duplicates: dict[int, str] = {}
    previous_key: str | None = None
    previous_index: int | None = None
    for i, seg in enumerate(segments):
        text = str(seg.get("text") or "").strip()
        key = " ".join(_normalize_token(w) for w in text.split()).strip()
        key_words = key.split()
        # Single words and very short interjections repeat legitimately.
        if key and len(key_words) >= 3 and previous_key is not None and previous_index is not None:
            prev_words = previous_key.split()
            is_retake = False
            if len(prev_words) >= 3:
                if previous_key == key:
                    is_retake = True
                elif key_words[: len(prev_words)] == prev_words:
                    is_retake = True
                elif SequenceMatcher(a=prev_words, b=key_words, autojunk=False).ratio() >= _RETAKE_SIMILARITY:
                    is_retake = True
            if is_retake:
                duplicates[previous_index] = str(segments[previous_index].get("text") or "").strip()
        previous_key = key or None
        previous_index = i
    return duplicates


def _clean_vad_silences(vad_silences: Any) -> list[tuple[float, float]]:
    """Validate raw silence ranges ([[s,e]] or [{"start","end"}]) into tuples."""
    if not isinstance(vad_silences, list):
        return []
    out: list[tuple[float, float]] = []
    for item in vad_silences:
        try:
            if isinstance(item, dict):
                start, end = float(item.get("start")), float(item.get("end"))
            else:
                start, end = float(item[0]), float(item[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if end > start:
            out.append((start, end))
    out.sort(key=lambda r: r[0])
    return out


def _detect_silences(
    segments: list[dict],
    duration: float,
    *,
    threshold: float,
    vad_silences: list[tuple[float, float]],
    dropped_segments: set[int],
) -> list[dict]:
    """Silence suggestions from real audio (VAD) or word-gap analysis.

    VAD silences come from Silero over the actual waveform and see pauses
    *inside* segments that Whisper merged — the segment-gap heuristic never
    could. Without VAD data, gaps between consecutive *word* timestamps are
    the next best signal (still finds intra-segment pauses when word
    timestamps are real); segments without words degrade to the historical
    segment-gap behaviour because their interpolated words butt against each
    other.

    A silence lying inside a segment that is already being dropped whole
    (bad take / retake) is skipped — cutting inside removed time is noise.
    """
    suggestions: list[dict] = []

    dropped_spans: list[tuple[float, float]] = []
    for i in dropped_segments:
        seg = segments[i]
        try:
            s, e = float(seg.get("start") or 0), float(seg.get("end") or 0)
        except (TypeError, ValueError):
            continue
        if e > s:
            dropped_spans.append((s, e))

    def _inside_dropped(a: float, b: float) -> bool:
        mid = (a + b) / 2
        return any(s <= mid <= e for s, e in dropped_spans)

    if vad_silences:
        for k, (raw_start, raw_end) in enumerate(vad_silences):
            gap = raw_end - raw_start
            if gap < threshold:
                continue
            cut_start = raw_start + _VAD_SILENCE_EDGE_KEEP
            cut_end = raw_end - _VAD_SILENCE_EDGE_KEEP
            if duration > 0:
                cut_end = min(cut_end, duration)
            if cut_end - cut_start < _MIN_SILENCE_CUT or _inside_dropped(cut_start, cut_end):
                continue
            suggestions.append({
                "id": f"silence-vad-{k}",
                "kind": "silence",
                "title": "Silence",
                "detail": f"{gap:.1f}s",
                "start": round(cut_start, 3),
                "end": round(cut_end, 3),
                "severity": "high" if gap > _SILENCE_HIGH_SEVERITY else "medium",
                "source": "vad",
            })
        return suggestions

    # Word-gap pass. Flatten every segment's word times in order; the gap
    # between one word's end and the next word's start is a candidate pause —
    # whether the two words share a segment or not.
    flat: list[tuple[float, float]] = []
    for i, seg in enumerate(segments):
        if i in dropped_segments:
            # Keep the segment's outer bounds so gaps *around* a dropped
            # segment still measure from real speech edges.
            try:
                s, e = float(seg.get("start") or 0), float(seg.get("end") or 0)
            except (TypeError, ValueError):
                continue
            if e > s:
                flat.append((s, e))
            continue
        timed = _segment_word_times(seg)
        if timed:
            flat.extend((s, e) for _, s, e in timed)

    silence_index = 0
    for k in range(1, len(flat)):
        prev_end = flat[k - 1][1]
        next_start = flat[k][0]
        gap = next_start - prev_end
        if gap < threshold:
            continue
        cut_start = prev_end + _SILENCE_EDGE_KEEP
        cut_end = next_start - _SILENCE_EDGE_KEEP
        if duration > 0:
            cut_end = min(cut_end, duration)
        if cut_end <= cut_start or _inside_dropped(cut_start, cut_end):
            continue
        silence_index += 1
        suggestions.append({
            "id": f"silence-{silence_index}",
            "kind": "silence",
            "title": "Silence",
            "detail": f"{gap:.1f}s",
            "start": round(cut_start, 3),
            "end": round(cut_end, 3),
            "severity": "high" if gap > _SILENCE_HIGH_SEVERITY else "medium",
            "source": "words",
        })
    return suggestions


def _detect_fillers(segments: list[dict], duration: float) -> list[dict]:
    """Filler-word suggestions with real word timing and detachment rules.

    Strong fillers (um/uh/…) are always proposed. Weak discourse words
    ("like", "so", "you know") are only proposed when detached — a
    ≥ _WEAK_DETACH_GAP pause on either side — since mid-flow they are
    usually legitimate grammar.
    """
    suggestions: list[dict] = []

    # Flattened (start, end) per token across segments, for cross-boundary
    # gap measurement around weak fillers.
    flat_times: list[tuple[float, float]] = []
    flat_ref: list[tuple[int, int]] = []  # (segment index, token index)
    per_segment: list[list[tuple[str, float, float]] | None] = []
    for i, seg in enumerate(segments):
        timed = _segment_word_times(seg)
        per_segment.append(timed)
        if timed:
            for j, (_, s, e) in enumerate(timed):
                flat_times.append((s, e))
                flat_ref.append((i, j))
    flat_pos = {ref: k for k, ref in enumerate(flat_ref)}

    def _detached(i: int, j: int) -> bool:
        k = flat_pos.get((i, j))
        if k is None:
            return False
        start, end = flat_times[k]
        gap_before = start - flat_times[k - 1][1] if k > 0 else float("inf")
        gap_after = flat_times[k + 1][0] - end if k + 1 < len(flat_times) else float("inf")
        return gap_before >= _WEAK_DETACH_GAP or gap_after >= _WEAK_DETACH_GAP

    def _bigram_detached(i: int, j: int) -> bool:
        k0 = flat_pos.get((i, j))
        k1 = flat_pos.get((i, j + 1))
        if k0 is None or k1 is None:
            return False
        gap_before = flat_times[k0][0] - flat_times[k0 - 1][1] if k0 > 0 else float("inf")
        gap_after = (
            flat_times[k1 + 1][0] - flat_times[k1][1] if k1 + 1 < len(flat_times) else float("inf")
        )
        return gap_before >= _WEAK_DETACH_GAP or gap_after >= _WEAK_DETACH_GAP

    def _clamp(ws: float, we: float) -> tuple[float, float]:
        lo = max(0.0, ws - 0.03)
        hi = we + 0.03
        if duration > 0:
            hi = min(duration, hi)
        return lo, hi

    for i, seg in enumerate(segments):
        timed = per_segment[i]
        if not timed:
            continue
        norms = [_normalize_token(w) for w, _, _ in timed]
        consumed: set[int] = set()

        for j in range(len(timed) - 1):
            if j in consumed or (norms[j], norms[j + 1]) not in _FILLER_BIGRAMS:
                continue
            if not _bigram_detached(i, j):
                continue
            ws, we = _clamp(timed[j][1], timed[j + 1][2])
            suggestions.append({
                "id": f"filler-{i}-{j}",
                "kind": "filler",
                "title": "Filler",
                "detail": f"{timed[j][0]} {timed[j + 1][0]}",
                "start": ws,
                "end": we,
                "severity": "medium",
            })
            consumed.update((j, j + 1))

        for j, (word, w_start, w_end) in enumerate(timed):
            if j in consumed:
                continue
            clean = norms[j].replace("'", "")
            if clean in _STRONG_FILLERS:
                severity = "high" if clean.startswith(("um", "uh")) else "medium"
            elif clean in _WEAK_FILLERS and _detached(i, j):
                severity = "medium"
            else:
                continue
            ws, we = _clamp(w_start, w_end)
            suggestions.append({
                "id": f"filler-{i}-{j}",
                "kind": "filler",
                "title": "Filler",
                "detail": word,
                "start": ws,
                "end": we,
                "severity": severity,
            })

    return suggestions


def _subtract_interval(
    ranges: list[dict], cut_start: float, cut_end: float, *, min_keep: float = _MIN_KEEP_FRAGMENT
) -> list[dict]:
    """Remove [cut_start, cut_end] from keep ranges, dropping sliver fragments."""
    if cut_end <= cut_start:
        return ranges
    out: list[dict] = []
    for rng in ranges:
        start, end = float(rng["start"]), float(rng["end"])
        if cut_end <= start or cut_start >= end:
            out.append(rng)
            continue
        if cut_start > start:
            left = {"start": start, "end": cut_start}
            if cut_start - start >= min_keep:
                out.append(left)
        if cut_end < end:
            right = {"start": cut_end, "end": end}
            if end - cut_end >= min_keep:
                out.append(right)
    return out


def _analyze_segments(
    segments: list[dict],
    duration: float,
    *,
    remove_fillers: bool = True,
    remove_silences: bool = True,
    remove_bad_takes: bool = True,
    remove_repeats: bool = True,
    aggressiveness: Aggressiveness = "balanced",
    vad_silences: Any = None,
) -> dict:
    """Server-side rough-cut analysis: derive keep_ranges and suggestion list from transcript.

    remove_fillers/remove_silences/remove_bad_takes gate whether each category
    contributes suggestions at all; a disabled category never reduces
    keepRanges either (its segments/gaps are treated as ordinary kept content).

    ``vad_silences`` — real audio silence ranges from Silero VAD
    (VideoTranscription.audio_analysis["silences"]). When present they replace
    transcript-gap inference entirely: the waveform, not the ASR's chunking,
    says where the dead air is.

    aggressiveness scales the silence gap threshold (the one clean continuous
    knob). Filler and bad-take detection are word-set / regex based and stay
    unscaled.
    """
    silence_threshold = _SILENCE_THRESHOLD_BY_AGGRESSIVENESS.get(aggressiveness, _SILENCE_THRESHOLD)
    keep: list[dict] = []
    suggestions: list[dict] = []
    dropped_segments: set[int] = set()

    # A sentence delivered twice (or nearly) in a row: the earlier copy leaves
    # keep_ranges entirely, the way a bad take does.
    duplicate_segments = _duplicate_segment_indices(segments) if remove_repeats else {}

    for i, seg in enumerate(segments):
        start = float(seg.get("start") or 0)
        end = float(seg.get("end") or start)
        text = str(seg.get("text") or "").strip()
        if end <= start:
            continue

        # Bad-take detection — mark as suggestion, exclude from keep (only when enabled)
        if remove_bad_takes and _BAD_TAKE_RE.search(text):
            dropped_segments.add(i)
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
            dropped_segments.add(i)
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

        keep.append({"start": start, "end": end})

    # Merge adjacent keep ranges
    merged: list[dict] = []
    for rng in sorted(keep, key=lambda r: r["start"]):
        if merged and rng["start"] <= merged[-1]["end"] + 0.03:
            merged[-1]["end"] = max(merged[-1]["end"], rng["end"])
        else:
            merged.append(dict(rng))

    if remove_silences:
        silence_suggestions = _detect_silences(
            segments,
            duration,
            threshold=silence_threshold,
            vad_silences=_clean_vad_silences(vad_silences),
            dropped_segments=dropped_segments,
        )
        suggestions.extend(silence_suggestions)
        # Silences are real cuts, not just annotations: subtract them so a
        # server-seeded draft (auto_apply) actually removes the dead air —
        # including pauses inside segments, which per-segment keeps never
        # excluded.
        for s in silence_suggestions:
            merged = _subtract_interval(merged, float(s["start"]), float(s["end"]))

    if remove_fillers:
        suggestions.extend(_detect_fillers(segments, duration))

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
    audio_analysis: dict[str, Any] | None = None,
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

        vad_silences = None
        if isinstance(audio_analysis, dict):
            vad_silences = audio_analysis.get("silences")
            if source_ranges and isinstance(vad_silences, list):
                lo, hi = source_ranges[0]["start"], source_ranges[0]["end"]
                clipped: list[list[float]] = []
                for rng in _clean_vad_silences(vad_silences):
                    s, e = max(rng[0], lo), min(rng[1], hi)
                    if e > s:
                        clipped.append([s, e])
                vad_silences = clipped

        analysis = _analyze_segments(
            scoped_segments,
            duration,
            remove_fillers=options.remove_fillers,
            remove_silences=options.remove_silences,
            remove_bad_takes=options.remove_bad_takes,
            remove_repeats=options.remove_repeats,
            aggressiveness=options.aggressiveness,
            vad_silences=vad_silences,
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
