"""
Clip suggestion: ask Gemini for viral moments from a transcript, fall back to
a deterministic rule-based scorer if no API key / parse fails.

Editube stores transcription segments as list[{start, end, text, speaker}].
We feed those timestamped lines to the LLM with an OpusClip-style prompt.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from typing import Any

from app.services.ai_client import generate_text

logger = logging.getLogger(__name__)


VALID_HOOKS = [
    "loud_words",
    "emotional_peak",
    "keywords",
    "subtlety",
    "aha_insight",
    "values_energy",
    "storytelling",
    "hot_take",
    "reaction",
    "pattern_interrupt",
]


@dataclass
class ClipSuggestion:
    start_time: float
    end_time: float
    duration: float
    virality_score: float
    reason: str
    transcript: str
    hooks_matched: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _build_prompt(
    transcript_lines: list[str],
    min_duration: float,
    max_duration: float,
    max_suggestions: int,
) -> str:
    transcript_block = "\n".join(transcript_lines)
    return f"""You are an expert viral content curator for TikTok, Instagram Reels, and YouTube Shorts.
Find the absolute BEST {max_suggestions} moments from the transcript below.

FINDER HOOKS — prioritize clips that match one or more of these signals:
1. LOUD WORDS / EMPHASIS — emphatic, punchy language, exclamations
2. EMOTIONAL PEAKS — passion, surprise, humor, vulnerability, excitement
3. KEYWORDS / BUZZWORDS — quotable phrases, trending terms, one-liners
4. SUBTLETY / NUANCE — insightful commentary revealing deeper thinking
5. CLEAR MOMENTS / AHA INSIGHTS — complex idea simplified into a shareable takeaway
6. VALUES / ENERGY — high-conviction statements, motivational intensity
7. STORYTELLING — mini-narrative with setup, tension, payoff
8. HOT TAKES — bold, polarizing, controversial opinions
9. REACTIONS / PUNCHLINES — spontaneous reactions, witty comebacks
10. PATTERN INTERRUPTS — unexpected shifts, tonal changes, surprises

CLIP QUALITY RULES:
- Hook within the first 2-3 seconds.
- Each clip = one complete, self-contained thought.
- Start/end on sentence or clause boundaries only (never mid-word).
- Duration: target 15-45 seconds; must be between {min_duration} and {max_duration} seconds.
- Rank by virality potential; return ONLY the best {max_suggestions}.
- Diverse topics/angles, no repetition.
- Trim aggressively: cut dead air, filler words, weak openings.

VIRALITY SCORING (0-100):
- 90-100 guaranteed viral (perfect hook + emotional peak + shareable insight)
- 70-89 strong (clear value + energy + complete thought)
- 50-69 decent (usable but needs context)
- Below 50 do not include

OUTPUT — return ONLY a valid JSON array (no markdown fences). Each element:
{{
  "start_time": number (seconds),
  "end_time": number (seconds),
  "reason": string,
  "virality_score": number 0-100,
  "transcript": string (exact text snippet),
  "hooks_matched": array of strings from {VALID_HOOKS}
}}

TRANSCRIPT (format: [start - end] text):
{transcript_block}
"""


def _parse_llm_json(raw: str) -> list[dict[str, Any]]:
    if not raw:
        return []
    text = raw.strip()
    # Strip ```json ... ``` fences if Gemini wraps the response despite instructions.
    fence = re.match(r"^```(?:json)?\s*(.+?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    # Tolerate leading text before the array.
    bracket = text.find("[")
    if bracket > 0:
        text = text[bracket:]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _validate(
    items: list[dict[str, Any]],
    min_duration: float,
    max_duration: float,
    video_duration: float | None,
) -> list[ClipSuggestion]:
    out: list[ClipSuggestion] = []
    for it in items:
        try:
            start = float(it.get("start_time"))
            end = float(it.get("end_time"))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        dur = end - start
        if dur < min_duration - 0.5 or dur > max_duration + 0.5:
            continue
        if video_duration and end > video_duration + 1:
            continue
        hooks = it.get("hooks_matched") or []
        if isinstance(hooks, str):
            hooks = [hooks]
        hooks = [h for h in hooks if isinstance(h, str) and h in VALID_HOOKS]
        score = it.get("virality_score")
        try:
            score_f = max(0.0, min(100.0, float(score) if score is not None else 70.0))
        except (TypeError, ValueError):
            score_f = 70.0
        out.append(
            ClipSuggestion(
                start_time=round(start, 2),
                end_time=round(end, 2),
                duration=round(dur, 2),
                virality_score=round(score_f, 1),
                reason=str(it.get("reason") or "")[:500],
                transcript=str(it.get("transcript") or "")[:2000],
                hooks_matched=hooks or ["keywords"],
            )
        )
    out.sort(key=lambda c: c.virality_score, reverse=True)
    return out


def _rule_based_fallback(
    segments: list[dict[str, Any]],
    min_duration: float,
    max_duration: float,
    max_suggestions: int,
) -> list[ClipSuggestion]:
    """
    Greedy windowing: walk segments, grow a window from min_duration up to max_duration
    on sentence boundaries, score by length + punctuation density + caps ratio.
    """
    out: list[ClipSuggestion] = []
    if not segments:
        return out

    norm = []
    for s in segments:
        try:
            norm.append(
                {
                    "start": float(s.get("start", 0.0)),
                    "end": float(s.get("end", 0.0)),
                    "text": str(s.get("text", "")).strip(),
                }
            )
        except (TypeError, ValueError):
            continue

    i = 0
    target = (min_duration + max_duration) / 2.0
    while i < len(norm) and len(out) < max_suggestions * 2:
        start = norm[i]["start"]
        j = i
        collected_text = []
        while j < len(norm) and (norm[j]["end"] - start) <= max_duration:
            collected_text.append(norm[j]["text"])
            dur = norm[j]["end"] - start
            if dur >= min_duration and dur >= target * 0.8:
                text = " ".join(collected_text).strip()
                hook_score = 60.0
                if re.search(r"[!?]", text):
                    hook_score += 8
                if sum(1 for c in text if c.isupper()) > len(text) * 0.05:
                    hook_score += 4
                if re.search(r"\b(never|always|secret|best|worst|shocking|truth)\b", text, re.I):
                    hook_score += 10
                out.append(
                    ClipSuggestion(
                        start_time=round(start, 2),
                        end_time=round(norm[j]["end"], 2),
                        duration=round(dur, 2),
                        virality_score=round(min(95.0, hook_score), 1),
                        reason="Heuristic match (no LLM key set)",
                        transcript=text[:2000],
                        hooks_matched=["keywords"],
                    )
                )
                i = j + 1
                break
            j += 1
        else:
            i += 1
            continue
    out.sort(key=lambda c: c.virality_score, reverse=True)
    return out[:max_suggestions]


def suggest_clips(
    segments: list[dict[str, Any]],
    *,
    min_duration: float = 15.0,
    max_duration: float = 60.0,
    max_suggestions: int = 8,
    video_duration: float | None = None,
) -> list[ClipSuggestion]:
    """
    Entry point. Returns up to max_suggestions ClipSuggestion instances,
    ranked by virality_score descending.
    """
    if not segments:
        return []

    lines = [
        f"[{float(s.get('start', 0)):.2f} - {float(s.get('end', 0)):.2f}] {str(s.get('text', '')).strip()}"
        for s in segments
        if str(s.get("text", "")).strip()
    ]
    if not lines:
        return []

    prompt = _build_prompt(lines, min_duration, max_duration, max_suggestions)
    try:
        raw = generate_text(prompt)
    except Exception as e:  # noqa: BLE001
        logger.warning("clip_analysis: LLM call failed (%s); falling back to rule-based", e)
        return _rule_based_fallback(segments, min_duration, max_duration, max_suggestions)

    parsed = _parse_llm_json(raw)
    validated = _validate(parsed, min_duration, max_duration, video_duration)
    if not validated:
        logger.info("clip_analysis: LLM returned no valid suggestions; using fallback")
        return _rule_based_fallback(segments, min_duration, max_duration, max_suggestions)
    return validated[:max_suggestions]
