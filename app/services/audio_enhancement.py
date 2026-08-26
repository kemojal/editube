"""Three-stem speech enhancement for rough-cut clips.

The high-quality local path combines Demucs (dialogue/music isolation) with
DeepFilterNet (full-band speech repair). Deployments can move the model behind
an HTTP worker with ``AUDIO_ENHANCE_PROVIDER=http``; a deterministic FFmpeg
fallback keeps the feature real on lightweight developer machines, but it is
not presented as model-parity quality.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


Progress = Callable[[int], None]


@dataclass(frozen=True)
class AudioEnhancementResult:
    path: Path
    provider: str


def _percent(value: Any, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(number):
        return fallback
    return max(0.0, min(1.0, number / 100.0))


def sanitize_audio_enhance_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    raw = settings if isinstance(settings, dict) else {}
    return {
        "speech": _percent(raw.get("speech"), 0.5),
        "music": _percent(raw.get("music"), 0.1),
        "background": _percent(raw.get("background"), 0.1),
        "normalize": raw.get("normalize") is not False,
    }


def build_stem_mix_filter(settings: dict[str, Any]) -> str:
    """Mix raw dialogue, repaired dialogue and music into one safe programme.

    Input 0 is isolated/raw dialogue, input 1 repaired dialogue, and input 2
    isolated music. The raw-minus-clean residual is the room/background stem.
    Keeping that residual separate is what makes the Background control real
    rather than another name for a global volume knob.
    """
    speech = float(settings["speech"])
    music = float(settings["music"])
    background = float(settings["background"])
    normalizer = ",loudnorm=I=-16:TP=-1.5:LRA=11" if settings["normalize"] else ""
    return (
        "[0:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
        "asplit=2[dialogue_mix][dialogue_residual];"
        "[1:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
        "asplit=2[clean_mix][clean_residual];"
        f"[dialogue_mix]volume={1.0 - speech:.6f}[dialogue_dry];"
        f"[clean_mix]volume={speech:.6f}[dialogue_wet];"
        "[dialogue_dry][dialogue_wet]amix=inputs=2:normalize=0[speech];"
        "[dialogue_residual][clean_residual]"
        "amix=inputs=2:weights='1 -1':normalize=0[room_raw];"
        f"[room_raw]volume={background:.6f}[room];"
        f"[2:a]volume={music:.6f}[music];"
        "[speech][room][music]amix=inputs=3:normalize=0:dropout_transition=0,"
        f"alimiter=limit=0.95{normalizer}[enhanced]"
    )


def render_audio_enhancement(
    source: str,
    clip_target: dict[str, Any],
    settings: dict[str, Any],
    output_path: Path,
    *,
    progress: Progress | None = None,
) -> AudioEnhancementResult:
    """Render one clip-local AAC enhancement suitable for preview and export."""
    clean_settings = sanitize_audio_enhance_settings(settings)
    start = max(0.0, _number(clip_target.get("start"), 0.0))
    end = max(start + 0.05, _number(clip_target.get("end"), start + 0.05))
    duration = end - start
    provider = os.environ.get("AUDIO_ENHANCE_PROVIDER", "auto").strip().lower() or "auto"
    if provider not in {"auto", "local", "ffmpeg", "http"}:
        raise RuntimeError("AUDIO_ENHANCE_PROVIDER must be auto, local, ffmpeg, or http")

    notify = progress or (lambda _value: None)
    with tempfile.TemporaryDirectory(prefix="editube-audio-") as tmp:
        work = Path(tmp)
        original = work / "original.wav"
        _run(
            [
                "ffmpeg", "-y", "-ss", f"{start:.6f}", "-i", source,
                "-t", f"{duration:.6f}", "-vn", "-ac", "2", "-ar", "48000",
                "-c:a", "pcm_s16le", str(original),
            ],
            "Unable to extract this clip's audio",
        )
        notify(18)

        if provider == "http":
            enhanced_mix = _render_http(original, clean_settings, work)
            used_provider = "http"
            notify(78)
        else:
            has_models = _has_module("demucs")
            if provider == "local" and not has_models:
                raise RuntimeError(
                    "Local Enhance Audio stem separation is not installed. Install requirements-ml.txt "
                    "or set AUDIO_ENHANCE_PROVIDER=http."
                )
            if provider != "ffmpeg" and has_models:
                try:
                    enhanced_mix, speech_provider = _render_local_models(original, clean_settings, work, notify)
                    used_provider = f"demucs+{speech_provider}"
                except RuntimeError:
                    if provider == "local":
                        raise
                    # Auto must stay usable on an offline worker whose package
                    # exists but whose Demucs weights have not been cached yet.
                    enhanced_mix = _render_ffmpeg_fallback(original, clean_settings, work)
                    used_provider = "ffmpeg-fallback"
                    notify(78)
            else:
                enhanced_mix = _render_ffmpeg_fallback(original, clean_settings, work)
                used_provider = "ffmpeg-fallback"
                notify(78)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        _run(
            [
                "ffmpeg", "-y", "-i", str(enhanced_mix), "-vn", "-c:a", "aac",
                "-b:a", "256k", "-ar", "48000", "-ac", "2", str(output_path),
            ],
            "Unable to encode the enhanced audio",
        )
        notify(92)
    return AudioEnhancementResult(path=output_path, provider=used_provider)


def _render_local_models(
    original: Path,
    settings: dict[str, Any],
    work: Path,
    progress: Progress,
) -> tuple[Path, str]:
    model = os.environ.get("AUDIO_DEMUCS_MODEL", "htdemucs").strip() or "htdemucs"
    separated = work / "separated"
    _run(
        [
            sys.executable, "-m", "demucs.separate", "--two-stems=vocals", "-n", model,
            "--shifts", "1", "--overlap", "0.25", "-o", str(separated), str(original),
        ],
        "Dialogue/music separation failed",
        timeout=_timeout("AUDIO_DEMUCS_TIMEOUT_SEC", 3600),
    )
    stem_dir = separated / model / original.stem
    dialogue = stem_dir / "vocals.wav"
    music = stem_dir / "no_vocals.wav"
    if not dialogue.exists() or not music.exists():
        raise RuntimeError("Dialogue/music separation finished without the expected stems")
    progress(52)

    deepfilter_python = _deepfilter_python()
    if deepfilter_python:
        repaired_dir = work / "repaired"
        repaired_dir.mkdir(parents=True, exist_ok=True)
        before = set(repaired_dir.glob("*.wav"))
        _run(
            [deepfilter_python, "-m", "df.enhance", "--output-dir", str(repaired_dir), str(dialogue)],
            "Speech repair failed",
            timeout=_timeout("AUDIO_DEEPFILTER_TIMEOUT_SEC", 1800),
        )
        repaired_files = sorted(
            set(repaired_dir.glob("*.wav")) - before,
            key=lambda path: path.stat().st_mtime,
        )
        if not repaired_files:
            repaired_files = sorted(repaired_dir.glob("*.wav"), key=lambda path: path.stat().st_mtime)
        if not repaired_files:
            raise RuntimeError("Speech repair finished without an output file")
        repaired = repaired_files[-1]
        speech_provider = "deepfilternet"
    else:
        repaired = _repair_speech_ffmpeg(dialogue, work / "speech-repaired.wav")
        speech_provider = "ffmpeg"
    progress(70)
    return _mix_stems(dialogue, repaired, music, settings, work / "enhanced.wav"), speech_provider


def _render_ffmpeg_fallback(original: Path, settings: dict[str, Any], work: Path) -> Path:
    repaired = _repair_speech_ffmpeg(original, work / "speech-repaired.wav")
    silence = work / "music-silence.wav"
    _run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-t", f"{_probe_duration(original):.6f}", "-c:a", "pcm_s16le", str(silence),
        ],
        "Unable to prepare the enhancement mix",
    )
    return _mix_stems(original, repaired, silence, settings, work / "enhanced.wav")


def _repair_speech_ffmpeg(source: Path, output: Path) -> Path:
    _run(
        [
            "ffmpeg", "-y", "-i", str(source),
            "-af", "highpass=f=70,lowpass=f=15500,afftdn=nr=16:nf=-35:tn=1,"
            "speechnorm=e=6.25:r=0.00001:l=1",
            "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(output),
        ],
        "Audio cleanup failed",
    )
    return output


def _mix_stems(dialogue: Path, repaired: Path, music: Path, settings: dict[str, Any], output: Path) -> Path:
    _run(
        [
            "ffmpeg", "-y", "-i", str(dialogue), "-i", str(repaired), "-i", str(music),
            "-filter_complex", build_stem_mix_filter(settings), "-map", "[enhanced]",
            "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", str(output),
        ],
        "Unable to mix enhanced audio stems",
    )
    return output


def _render_http(original: Path, settings: dict[str, Any], work: Path) -> Path:
    url = os.environ.get("AUDIO_ENHANCE_URL", "").strip()
    if not url:
        raise RuntimeError("AUDIO_ENHANCE_URL is required for the HTTP audio provider")
    import httpx

    timeout = _timeout("AUDIO_ENHANCE_HTTP_TIMEOUT_SEC", 1800)
    with original.open("rb") as handle:
        response = httpx.post(
            url,
            files={"file": (original.name, handle, "audio/wav")},
            data={"settings": json.dumps(settings, separators=(",", ":"))},
            timeout=timeout,
        )
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()
    if "audio" not in content_type and "octet-stream" not in content_type:
        raise RuntimeError("The audio enhancement provider returned a non-audio response")
    output = work / "http-enhanced.wav"
    output.write_bytes(response.content)
    return output


def _run(command: list[str], label: str, *, timeout: int = 900) -> None:
    try:
        proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{label}: timed out") from exc
    if proc.returncode:
        tail = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()[-4:]
        raise RuntimeError(f"{label}: {' '.join(tail)[:600]}")


def _probe_duration(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return max(0.05, float(proc.stdout.decode().strip()))


def _has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _deepfilter_python() -> str | None:
    """Find the isolated DeepFilterNet runtime without importing its NumPy 1.x stack."""
    project_root = Path(__file__).resolve().parents[2]
    candidates = [
        os.environ.get("AUDIO_DEEPFILTER_PYTHON", "").strip(),
        str(project_root / ".venv-audio-enhance" / "bin" / "python"),
        sys.executable if _has_module("df") else "",
    ]
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        check = subprocess.run(
            [candidate, "-c", "import df"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if check.returncode == 0:
            return candidate
    return None


def _timeout(name: str, fallback: int) -> int:
    try:
        return max(30, int(os.environ.get(name, str(fallback))))
    except (TypeError, ValueError):
        return fallback


def _number(value: Any, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if math.isfinite(number) else fallback
