"""Audio-level speech/silence analysis via Silero VAD.

Transcript segments alone cannot say where silence really is: Whisper merges
long pauses into one segment (word gaps are invisible at segment level) and
its timestamps drift at boundaries. This runs the Silero VAD bundled with
faster-whisper directly over the already-extracted 16 kHz mono WAV and stores
real speech ranges — the silences are their complement.

Parameters are editorial, not ASR-chunking, defaults: Whisper's own VAD
pre-filter uses min_silence=2000ms / pad=400ms because it only needs coarse
chunks; for cutting dead air out of a video we need every pause down to a
quarter second, with just enough padding to protect word onsets (attack of
plosives) — the same onset/hangover idea Handy's smoothed Silero VAD uses.

Never raises out of `analyze_wav_speech`: audio analysis is an enhancement,
and a failure must not take down the transcription job that hosts it.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 16000

# Editorial VAD parameters (env-overridable).
_DEF_THRESHOLD = 0.5            # Silero speech probability (AUDIO_VAD_THRESHOLD)
_DEF_MIN_SPEECH_MS = 60         # ignore blips shorter than this (AUDIO_VAD_MIN_SPEECH_MS)
_DEF_MIN_SILENCE_MS = 250       # split speech on pauses this long (AUDIO_VAD_MIN_SILENCE_MS)
_DEF_SPEECH_PAD_MS = 100        # pre/post-roll protecting word edges (AUDIO_VAD_SPEECH_PAD_MS)
_MIN_REPORTED_SILENCE_S = 0.25  # complement gaps shorter than this are just word spacing
_MAX_RANGES = 5000              # JSONB size guard for multi-hour sources
_PEAK_BARS = 3200               # waveform buckets (AUDIO_PEAK_BARS); matches the editor's client extractor


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def analyze_wav_speech(
    wav_path: str | Path,
    *,
    offset_seconds: float = 0.0,
) -> dict[str, Any] | None:
    """Speech ranges + silences for a 16 kHz mono WAV, in source-video seconds.

    ``offset_seconds`` is the source-range start the WAV was extracted from
    (the same offset the transcription job adds to segment times), so stored
    ranges line up with segment/word timestamps.

    Returns None (and logs) on any failure or when disabled via
    ``AUDIO_ANALYSIS_ENABLED=0``.
    """
    enabled = os.environ.get("AUDIO_ANALYSIS_ENABLED", "1").strip().lower() not in {"0", "false", "no"}
    if not enabled:
        return None
    try:
        from faster_whisper.audio import decode_audio
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        audio = decode_audio(str(wav_path), sampling_rate=_SAMPLE_RATE)
        total = float(len(audio)) / _SAMPLE_RATE
        if total <= 0:
            return None

        threshold = _env_float("AUDIO_VAD_THRESHOLD", _DEF_THRESHOLD)
        min_speech_ms = _env_int("AUDIO_VAD_MIN_SPEECH_MS", _DEF_MIN_SPEECH_MS)
        min_silence_ms = _env_int("AUDIO_VAD_MIN_SILENCE_MS", _DEF_MIN_SILENCE_MS)
        speech_pad_ms = _env_int("AUDIO_VAD_SPEECH_PAD_MS", _DEF_SPEECH_PAD_MS)

        options = VadOptions(
            threshold=threshold,
            min_speech_duration_ms=min_speech_ms,
            min_silence_duration_ms=min_silence_ms,
            speech_pad_ms=speech_pad_ms,
        )
        raw = get_speech_timestamps(audio, options, sampling_rate=_SAMPLE_RATE)

        speech: list[list[float]] = []
        for item in raw:
            start = max(0.0, float(item["start"]) / _SAMPLE_RATE)
            end = min(total, float(item["end"]) / _SAMPLE_RATE)
            if end > start:
                speech.append([start, end])
        speech.sort(key=lambda r: r[0])

        silences: list[list[float]] = []
        cursor = 0.0
        for start, end in speech:
            if start - cursor >= _MIN_REPORTED_SILENCE_S:
                silences.append([cursor, start])
            cursor = max(cursor, end)
        if total - cursor >= _MIN_REPORTED_SILENCE_S:
            silences.append([cursor, total])

        def _offset(ranges: list[list[float]]) -> list[list[float]]:
            return [
                [round(s + offset_seconds, 3), round(e + offset_seconds, 3)]
                for s, e in ranges[:_MAX_RANGES]
            ]

        result: dict[str, Any] = {
            "version": 1,
            "engine": "silero-vad",
            "sample_rate": _SAMPLE_RATE,
            "duration": round(total + offset_seconds, 3),
            "speech_ranges": _offset(speech),
            "silences": _offset(silences),
            "params": {
                "threshold": threshold,
                "min_speech_duration_ms": min_speech_ms,
                "min_silence_duration_ms": min_silence_ms,
                "speech_pad_ms": speech_pad_ms,
            },
        }

        # Waveform peaks, so the editor never has to download and decode the
        # whole master file client-side just to paint the timeline waveform.
        # Only for full-source extractions: a range-trimmed WAV (offset > 0)
        # would misalign against the player's timeline.
        if offset_seconds == 0:
            peaks = _waveform_peaks(audio)
            if peaks:
                result["peaks"] = peaks

        return result
    except Exception:
        logger.exception("Audio speech analysis failed for %s", wav_path)
        return None


def _waveform_peaks(audio: Any) -> list[float] | None:
    """Per-bucket max-abs amplitude, normalized to this file's own ceiling.

    Linear 0..1 values; the editor applies its perceptual display curve. Never
    raises — peaks are an enhancement on top of the VAD result.
    """
    try:
        import numpy as np

        bars = _env_int("AUDIO_PEAK_BARS", _PEAK_BARS)
        samples = np.asarray(audio)
        if bars <= 0 or samples.size < bars:
            return None
        block = samples.size // bars
        trimmed = np.abs(samples[: block * bars]).reshape(bars, block)
        raw = trimmed.max(axis=1)
        ceiling = max(float(raw.max()), 0.01)
        return [round(float(value) / ceiling, 3) for value in raw]
    except Exception:
        logger.exception("Waveform peak extraction failed")
        return None
