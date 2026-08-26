"""Fuzzy alignment between transcript text tokens and timed ASR words.

Whisper's word list rarely matches ``text.split()`` one-to-one: it splits
hyphenated compounds ("cross-platform." arrives as "cross" + "-platform."),
re-tokenizes contractions, and users later edit the text outright. Every
consumer used to guard on ``len(words) == len(tokens)`` and throw away *all*
real timings on any mismatch, falling back to even division — which erases
real pauses inside the segment and mistimes every cut.

This module aligns the two sequences instead:

1. Exact anchors via ``difflib.SequenceMatcher`` on normalized tokens.
2. Inside each unmatched gap, a concat pass resolves 1↔N splits (one text
   token spanning several ASR words, or one ASR word spanning several text
   tokens).
3. Whatever is still unmatched is interpolated character-proportionally
   between the nearest timed neighbours, then clamped monotonic.

The result always has exactly one ``(start, end)`` per text token, so the
"words[i] belongs to tokens[i]" invariant downstream code wants is guaranteed
by construction rather than by rejection.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

_MIN_WORD_SECONDS = 0.01

_NORM_RE = re.compile(r"[^a-z0-9]+")


def normalize_word(text: str) -> str:
    """Case/punctuation-insensitive form used for matching ("‑Platform." → "platform")."""
    return _NORM_RE.sub("", (text or "").lower())


def _clean_timed_words(words: Any) -> list[tuple[str, float, float]]:
    """Validate raw ``[{word,start,end}]`` into ordered (text, start, end) triples."""
    if not isinstance(words, list) or not words:
        return []
    out: list[tuple[str, float, float]] = []
    for raw in words:
        if not isinstance(raw, dict):
            return []
        try:
            text = str(raw.get("word") or "")
            start = float(raw.get("start"))
            end = float(raw.get("end"))
        except (TypeError, ValueError):
            return []
        if end < start:
            return []
        out.append((text, start, end))
    # ASR words arrive in spoken order; a violation means the data is untrustworthy.
    for i in range(1, len(out)):
        if out[i][1] < out[i - 1][1] - 0.05:
            return []
    return out


def align_tokens_to_words(
    tokens: list[str],
    words: Any,
    *,
    seg_start: float,
    seg_end: float,
) -> list[dict[str, Any]]:
    """One ``{"start","end","matched"}`` per token, in token order.

    ``matched`` is True when the token's time came from a real ASR word (or a
    concatenation of them) rather than interpolation.
    """
    n = len(tokens)
    if n == 0:
        return []
    if seg_end < seg_start:
        seg_end = seg_start

    timed = _clean_timed_words(words)
    token_norms = [normalize_word(t) for t in tokens]
    word_norms = [normalize_word(w) for w, _, _ in timed]

    slots: list[tuple[float, float] | None] = [None] * n

    if timed:
        matcher = SequenceMatcher(a=token_norms, b=word_norms, autojunk=False)
        blocks = [b for b in matcher.get_matching_blocks() if b.size > 0]

        for block in blocks:
            for k in range(block.size):
                ti, wi = block.a + k, block.b + k
                if token_norms[ti]:  # never anchor on empty normals (pure punctuation)
                    slots[ti] = (timed[wi][1], timed[wi][2])

        # Unanchored gaps between consecutive blocks (plus head and tail).
        gaps: list[tuple[int, int, int, int]] = []
        prev_a, prev_b = 0, 0
        for block in blocks:
            gaps.append((prev_a, block.a, prev_b, block.b))
            prev_a, prev_b = block.a + block.size, block.b + block.size
        gaps.append((prev_a, n, prev_b, len(timed)))

        for a0, a1, b0, b1 in gaps:
            _align_gap(token_norms, timed, word_norms, slots, a0, a1, b0, b1)

    matched = [slots[i] is not None for i in range(n)]
    _interpolate_unmatched(tokens, slots, seg_start, seg_end)
    _enforce_monotonic(slots, seg_start, seg_end)

    return [
        {"start": slots[i][0], "end": slots[i][1], "matched": matched[i]}
        for i in range(n)
    ]


def _align_gap(
    token_norms: list[str],
    timed: list[tuple[str, float, float]],
    word_norms: list[str],
    slots: list[tuple[float, float] | None],
    a0: int,
    a1: int,
    b0: int,
    b1: int,
) -> None:
    """Resolve 1↔N concatenation matches inside an unanchored gap.

    Handles the common ASR tokenization drift: one text token equals the
    concatenation of several ASR words ("crossplatform" == "cross"+"platform"),
    or one ASR word equals several text tokens. Tokens and words that stay
    unmatched but face each other in the same gap are a rewrite of the same
    audio, so the tokens are spread over those words' time envelope rather
    than left for anchor-to-anchor interpolation.
    """
    used_words: set[int] = set()
    i, j = a0, b0
    while i < a1:
        tn = token_norms[i]
        if not tn:
            i += 1
            continue

        anchored = False
        jj = j
        while jj < b1 and not anchored:
            wn = word_norms[jj]
            if not wn:
                jj += 1
                continue

            if wn == tn:
                slots[i] = (timed[jj][1], timed[jj][2])
                used_words.add(jj)
                j = jj + 1
                i += 1
                anchored = True
                break

            # One token == concat of the next few ASR words.
            concat_w = wn
            kk = jj
            while kk + 1 < b1 and len(concat_w) < len(tn) and kk - jj < 3:
                kk += 1
                concat_w += word_norms[kk]
                if concat_w == tn:
                    slots[i] = (timed[jj][1], timed[kk][2])
                    used_words.update(range(jj, kk + 1))
                    j = kk + 1
                    i += 1
                    anchored = True
                    break
            if anchored:
                break

            # One ASR word == concat of the next few tokens: split its span
            # among them character-proportionally.
            concat_t = tn
            ii = i
            while ii + 1 < a1 and len(concat_t) < len(wn) and ii - i < 3:
                ii += 1
                concat_t += token_norms[ii]
                if concat_t == wn:
                    w_start, w_end = timed[jj][1], timed[jj][2]
                    span = max(w_end - w_start, _MIN_WORD_SECONDS)
                    total_chars = sum(max(len(token_norms[k]), 1) for k in range(i, ii + 1))
                    cursor = w_start
                    for k in range(i, ii + 1):
                        share = span * (max(len(token_norms[k]), 1) / total_chars)
                        slots[k] = (cursor, min(cursor + share, w_end))
                        cursor += share
                    used_words.add(jj)
                    j = jj + 1
                    i = ii + 1
                    anchored = True
                    break
            if anchored:
                break

            jj += 1

        if not anchored:
            i += 1  # token has no counterpart here; j stays for the next token

    unmatched_i = [k for k in range(a0, a1) if slots[k] is None and token_norms[k]]
    unmatched_j = [k for k in range(b0, b1) if k not in used_words and word_norms[k]]

    # Rewritten span: give the replacement tokens the replaced words' envelope.
    if unmatched_i and unmatched_j:
        env_start = timed[unmatched_j[0]][1]
        env_end = max(timed[k][2] for k in unmatched_j)
        span = max(env_end - env_start, _MIN_WORD_SECONDS)
        total_chars = sum(max(len(token_norms[k]), 1) for k in unmatched_i)
        cursor = env_start
        for k in unmatched_i:
            share = span * (max(len(token_norms[k]), 1) / total_chars)
            slots[k] = (cursor, min(cursor + share, env_end))
            cursor += share


def _interpolate_unmatched(
    tokens: list[str],
    slots: list[tuple[float, float] | None],
    seg_start: float,
    seg_end: float,
) -> None:
    """Fill unmatched runs character-proportionally between timed neighbours."""
    n = len(tokens)
    i = 0
    while i < n:
        if slots[i] is not None:
            i += 1
            continue
        run_start = i
        while i < n and slots[i] is None:
            i += 1
        run_end = i  # exclusive

        left = slots[run_start - 1][1] if run_start > 0 else seg_start
        right = slots[run_end][0] if run_end < n else seg_end
        if right < left:
            right = left

        total_chars = sum(max(len(tokens[k]), 1) for k in range(run_start, run_end))
        span = right - left
        cursor = left
        for k in range(run_start, run_end):
            share = span * (max(len(tokens[k]), 1) / total_chars) if total_chars else 0.0
            slots[k] = (cursor, cursor + share)
            cursor += share


def _enforce_monotonic(
    slots: list[tuple[float, float] | None],
    seg_start: float,
    seg_end: float,
) -> None:
    """Clamp into segment bounds; starts must never run backwards."""
    hard_end = max(seg_end, seg_start + _MIN_WORD_SECONDS)
    prev_start = seg_start
    for i, slot in enumerate(slots):
        start, end = slot  # every slot is filled by now
        start = max(start, prev_start, seg_start)
        end = max(end, start + _MIN_WORD_SECONDS)
        if end > hard_end:
            end = hard_end
            start = max(seg_start, min(start, end - _MIN_WORD_SECONDS))
        slots[i] = (start, end)
        prev_start = start


def timed_words_for_tokens(
    tokens: list[str],
    words: Any,
    *,
    seg_start: float,
    seg_end: float,
) -> list[tuple[str, float, float]]:
    """(token, start, end) per token — the auto-edit view of a segment."""
    aligned = align_tokens_to_words(tokens, words, seg_start=seg_start, seg_end=seg_end)
    return [(tokens[i], aligned[i]["start"], aligned[i]["end"]) for i in range(len(tokens))]


def realign_words_to_text(
    new_text: str,
    old_words: Any,
    *,
    seg_start: float,
    seg_end: float,
) -> list[dict[str, Any]] | None:
    """New ``words`` array for an edited segment, preserving real timings.

    Tokens of the corrected text that also appear (in order) in the original
    ASR words keep those words' timings; inserted/rewritten spans are
    interpolated between the surviving anchors. Returns None when the segment
    has no usable timed words at all — the caller should then drop ``words``
    (nothing real to preserve).
    """
    tokens = new_text.split()
    if not tokens:
        return None
    if not _clean_timed_words(old_words):
        return None
    aligned = align_tokens_to_words(tokens, old_words, seg_start=seg_start, seg_end=seg_end)
    return [
        {
            "word": tokens[i],
            "start": round(aligned[i]["start"], 3),
            "end": round(aligned[i]["end"], 3),
        }
        for i in range(len(tokens))
    ]
