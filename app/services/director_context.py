"""What the director reads, and the clock it reads it in.

Three clocks are in play and confusing them is the likeliest way this feature
ships subtly broken (docs/ai_creative_director.md §6):

* **source time** — seconds in the uploaded file. `keepRanges`, transcript
  segments and `timelineMediaItems.start/end` are all in this.
* **director time** — seconds in the *cut*, i.e. the kept ranges concatenated.
  This is the only clock the model ever sees, because it is the only one that
  describes the video a viewer would watch.
* **export time** — seconds in the rendered MP4. `rough_cut_export` derives it;
  nothing here needs it.

`CutMap` is the piecewise-linear map between the first two. Everything the model
emits comes back in director time and has to be converted before it can be
placed, and a shot converted with the wrong map lands on the wrong sentence.

The other half of this module mirrors the editor's own word derivation
(`segmentsToWords` in `_lib/rough-cut-utils.ts`) exactly — same ids, same
timings, same fallbacks. That is deliberate: an anchor resolved here has to point
at the words the editor will highlight, or the plan panel and the timeline
disagree about what a shot is attached to.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

logger = logging.getLogger(__name__)

_WHITESPACE = re.compile(r"\s+")
_EDGE_PUNCTUATION = re.compile(r"^[^\w']+|[^\w']+$")


def _segment_text(segment: dict[str, Any]) -> str:
    """Mirror of `segmentText`: collapse whitespace, trim."""
    return _WHITESPACE.sub(" ", str(segment.get("text") or "")).strip()


def normalize_token(text: str) -> str:
    """Mirror of `normalizeWord`: lowercase, strip edge punctuation."""
    return _EDGE_PUNCTUATION.sub("", text.lower())


def resolve_segment_word_timings(
    segment: dict[str, Any], tokens: list[str]
) -> list[tuple[float, float]] | None:
    """Real ASR word timings, when they can be trusted.

    Mirror of `lib/transcript/word-timings.ts`, which is itself a mirror of the
    guard in `auto_edit.py`. Any violation invalidates the *whole* segment
    rather than one word: a partially-real timing list interleaved with
    synthesised ones drifts in ways nobody can debug later.
    """
    words = segment.get("words")
    if not isinstance(words, list) or not words or len(words) != len(tokens):
        return None

    resolved: list[tuple[float, float]] = []
    previous_end = float("-inf")
    for word in words:
        if not isinstance(word, dict):
            return None
        start, end = word.get("start"), word.get("end")
        # Explicit numbers only — `float(None)` would coerce where the editor
        # rejects, and the two must agree.
        if not isinstance(start, (int, float)) or isinstance(start, bool):
            return None
        if not isinstance(end, (int, float)) or isinstance(end, bool):
            return None
        start, end = float(start), float(end)
        if start != start or end != end:  # NaN
            return None
        # 0.05s of tolerance for ASR jitter, then monotonicity is required.
        if start < previous_end - 0.05 or end < start:
            return None
        resolved.append((start, end))
        previous_end = end
    return resolved


@dataclass(frozen=True)
class Word:
    """One transcript word, in source time, with the editor's own id."""

    id: str
    segment_index: int
    text: str
    start: float
    end: float
    speaker: str | None = None


def segments_to_words(segments: Iterable[dict[str, Any]]) -> list[Word]:
    """Mirror of `segmentsToWords`, ids included.

    The id is `{segmentIndex}-{wordIndex}-{segmentStart:.2f}` and the index is
    over the *original* array — a segment with no text is skipped but still
    consumes its index, exactly as the editor's `forEach` does. Getting that
    wrong would shift every id after the first empty segment.
    """
    words: list[Word] = []
    for segment_index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            continue
        text = _segment_text(segment)
        if not text:
            continue
        parts = [part for part in text.split(" ") if part]
        if not parts:
            continue

        try:
            start = max(0.0, float(segment.get("start") or 0.0))
        except (TypeError, ValueError):
            start = 0.0
        try:
            raw_end = float(segment.get("end") or 0.0)
        except (TypeError, ValueError):
            raw_end = 0.0
        end = max(start + 0.2, raw_end or (start + len(parts) * 0.36))
        duration = end - start
        total_chars = sum(max(1, len(part)) for part in parts)

        real = resolve_segment_word_timings(segment, parts)
        cursor = start
        speaker = segment.get("speaker")

        for index, part in enumerate(parts):
            if real is not None:
                word_start = min(max(real[index][0], start), end)
                word_end = min(max(max(real[index][1], word_start), start), end)
            else:
                share = max(0.12, duration * (max(1, len(part)) / total_chars))
                word_start = cursor
                word_end = end if index == len(parts) - 1 else min(end, cursor + share)
                cursor = word_end
            words.append(
                Word(
                    id=f"{segment_index}-{index}-{start:.2f}",
                    segment_index=segment_index,
                    text=part,
                    start=word_start,
                    end=word_end,
                    speaker=str(speaker) if speaker else None,
                )
            )
    return words


@dataclass(frozen=True)
class CutMap:
    """The piecewise-linear map between source time and director time."""

    ranges: tuple[tuple[float, float], ...]

    @classmethod
    def from_keep_ranges(
        cls, raw: Any, *, source_duration: float
    ) -> "CutMap":
        """Build from a draft's `keepRanges`.

        An empty or unusable list means nothing was cut — which is what the
        editor means by it too (`isWholeTakeTimeline` treats empty as the whole
        take), so it maps to a single range covering the source rather than to
        an empty timeline.
        """
        parsed: list[tuple[float, float]] = []
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            try:
                start = float(item.get("start", 0.0))
                end = float(item.get("end", 0.0))
            except (TypeError, ValueError):
                continue
            start = max(0.0, min(start, source_duration))
            end = max(0.0, min(end, source_duration))
            if end - start > 0.001:
                parsed.append((start, end))

        if not parsed:
            return cls(ranges=((0.0, max(0.0, source_duration)),))

        parsed.sort()
        merged: list[tuple[float, float]] = [parsed[0]]
        for start, end in parsed[1:]:
            last_start, last_end = merged[-1]
            # Overlapping kept ranges would double-count time and desynchronise
            # every conversion after them.
            if start <= last_end:
                merged[-1] = (last_start, max(last_end, end))
            else:
                merged.append((start, end))
        return cls(ranges=tuple(merged))

    @property
    def runtime(self) -> float:
        """Length of the cut — the director's whole world."""
        return sum(end - start for start, end in self.ranges)

    def to_director(self, source_seconds: float) -> float | None:
        """Where a source moment lands in the cut, or None if it was removed."""
        offset = 0.0
        for start, end in self.ranges:
            if source_seconds < start:
                return None  # inside a removed gap
            if source_seconds <= end:
                return offset + (source_seconds - start)
            offset += end - start
        return None

    def to_source(self, director_seconds: float) -> float:
        """Where a moment in the cut came from.

        Clamped rather than optional: every director-time value the model can
        produce is inside the cut by construction, so there is no meaningful
        "nowhere" to return, and clamping keeps a rounding error at the tail
        from losing a shot.
        """
        remaining = max(0.0, director_seconds)
        for start, end in self.ranges:
            span = end - start
            if remaining <= span:
                return start + remaining
            remaining -= span
        return self.ranges[-1][1]

    #: Below this a word is a boundary artefact rather than something audible.
    _MIN_AUDIBLE_OVERLAP = 0.02

    def _best_range(self, start: float, end: float) -> tuple[tuple[float, float] | None, float]:
        """The kept range this span mostly belongs to, and by how much."""
        best: tuple[float, float] | None = None
        best_overlap = 0.0
        for range_start, range_end in self.ranges:
            overlap = min(end, range_end) - max(start, range_start)
            if overlap > best_overlap:
                best, best_overlap = (range_start, range_end), overlap
        return best, best_overlap

    def director_words(self, words: list[Word]) -> list[tuple[Word, float, float]]:
        """Every surviving word with its director-time span.

        Survival is decided by *overlap*, not by whether the word's start
        happens to land inside a range. A word beginning exactly where a cut
        ends touches the kept range for zero seconds — it was removed — but a
        containment test says it survived, and the model is then shown a line of
        transcript that no longer exists.

        A word straddling a boundary is kept and clipped to the side it mostly
        falls on, which is the same call the cut itself made.
        """
        out: list[tuple[Word, float, float]] = []
        for word in words:
            # Guard against zero-length ASR words, which would never overlap.
            span_end = max(word.end, word.start + 0.001)
            kept_range, overlap = self._best_range(word.start, span_end)
            if kept_range is None or overlap <= self._MIN_AUDIBLE_OVERLAP:
                continue
            clipped_start = max(word.start, kept_range[0])
            clipped_end = min(span_end, kept_range[1])
            director_start = self.to_director(clipped_start)
            if director_start is None:
                continue
            director_end = self.to_director(clipped_end)
            if director_end is None or director_end < director_start:
                director_end = director_start
            out.append((word, director_start, director_end))
        return out


@dataclass
class DirectorContext:
    """Everything the model is given about this video."""

    transcript: str
    runtime_seconds: float
    aspect: str
    segment_ids: set[str]
    words: list[Word]
    cut_map: CutMap
    #: Surviving words only — what the model was shown.
    words_by_segment: dict[int, list[Word]]
    #: Every word, including those the cut removed. Only used to tell "these
    #: words were cut" from "these words were never said", which is the
    #: difference between a warning the user can act on and one they cannot.
    all_words_by_segment: dict[int, list[Word]] = field(default_factory=dict)

    @property
    def has_speech(self) -> bool:
        return bool(self.words)

    def was_cut(self, quote: str) -> bool:
        """Whether a quote exists in the source but not in the cut."""
        if not quote.strip():
            return False
        for words in self.all_words_by_segment.values():
            if find_quote(words, quote):
                break
        else:
            return False
        return not any(find_quote(words, quote) for words in self.words_by_segment.values())


def build_context(
    *,
    segments: list[dict[str, Any]],
    keep_ranges: Any,
    source_duration: float,
    aspect: str,
) -> DirectorContext:
    """Assemble the transcript the director reads, in director time."""
    words = segments_to_words(segments)
    cut_map = CutMap.from_keep_ranges(keep_ranges, source_duration=source_duration)

    # Carry the director times `director_words` already worked out rather than
    # recomputing them from each word's raw source span. The two are not the
    # same: a surviving word can *start* inside a removed gap and only overlap
    # the kept range partway through, so `to_director(word.start)` is None even
    # though the word is on screen. Recomputing here dropped whole segments on
    # that None and collapsed others to zero length — the boundary case
    # `director_words` exists to handle, defeated by second-guessing it.
    by_segment: dict[int, list[Word]] = {}
    spans: dict[int, list[tuple[float, float]]] = {}
    for word, director_start, director_end in cut_map.director_words(words):
        by_segment.setdefault(word.segment_index, []).append(word)
        spans.setdefault(word.segment_index, []).append((director_start, director_end))

    lines: list[str] = []
    segment_ids: set[str] = set()
    for segment_index, segment_words in sorted(by_segment.items()):
        segment_spans = spans.get(segment_index) or []
        if not segment_words or not segment_spans:
            continue
        start = min(span[0] for span in segment_spans)
        end = max(max(span[1] for span in segment_spans), start)
        segment_id = f"s{segment_index}"
        segment_ids.add(segment_id)
        speaker = segment_words[0].speaker
        who = f" {speaker}" if speaker else ""
        text = " ".join(word.text for word in segment_words)
        lines.append(f"[{segment_id}] {start:.2f}–{end:.2f}{who}: {text}")

    all_by_segment: dict[int, list[Word]] = {}
    for word in words:
        all_by_segment.setdefault(word.segment_index, []).append(word)

    return DirectorContext(
        transcript="\n".join(lines),
        runtime_seconds=cut_map.runtime,
        aspect=aspect,
        segment_ids=segment_ids,
        words=words,
        cut_map=cut_map,
        words_by_segment=by_segment,
        all_words_by_segment=all_by_segment,
    )


def find_quote(words: list[Word], quote: str) -> tuple[int, int] | None:
    """Locate a quote inside a segment's words, as an index range.

    Matching is on normalised tokens rather than raw characters: the model is
    asked to copy verbatim and mostly does, but punctuation and capitalisation
    drift constantly and neither changes which words were said.
    """
    needle = [normalize_token(part) for part in quote.split() if normalize_token(part)]
    if not needle:
        return None
    haystack = [normalize_token(word.text) for word in words]
    for offset in range(len(haystack) - len(needle) + 1):
        if haystack[offset : offset + len(needle)] == needle:
            return offset, offset + len(needle) - 1
    return None


@dataclass(frozen=True)
class ResolvedAnchor:
    """Where a directive actually attaches, in source seconds."""

    start: float
    end: float
    exact: bool


def resolve_anchor(
    context: DirectorContext,
    *,
    segment_id: str,
    quote: str,
    fallback_director_start: float,
    fallback_director_end: float,
) -> ResolvedAnchor:
    """Turn an anchor into source seconds.

    Anchor first, timing second, and for a reason: the anchor is what survives a
    later re-cut. If the user trims another ten seconds out of the middle, every
    director-time number in the plan silently means something else, while
    "the shot over 'two days a week'" still means what it said.

    The timing fallback is marked `exact=False` so callers can warn rather than
    quietly placing a shot somewhere approximate.
    """
    try:
        index = int(str(segment_id).lstrip("s"))
    except (TypeError, ValueError):
        index = -1

    words = context.words_by_segment.get(index)
    if words:
        found = find_quote(words, quote)
        if found:
            first, last = found
            return ResolvedAnchor(start=words[first].start, end=words[last].end, exact=True)

    # The quote was not where the model said it was. Search the whole transcript
    # before giving up — a misattributed segment id is a much smaller error than
    # a misquoted line, and the quote alone still identifies the moment.
    for candidate_words in context.words_by_segment.values():
        found = find_quote(candidate_words, quote)
        if found:
            first, last = found
            return ResolvedAnchor(
                start=candidate_words[first].start, end=candidate_words[last].end, exact=True
            )

    return ResolvedAnchor(
        start=context.cut_map.to_source(fallback_director_start),
        end=context.cut_map.to_source(fallback_director_end),
        exact=False,
    )
