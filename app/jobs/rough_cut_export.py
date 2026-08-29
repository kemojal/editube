"""
RQ job: concatenate rough-cut keepRanges via ffmpeg and upload to Cloudinary.

Supports: frameRate from exportSettings, optional burned-in subtitles from DB
transcription + keepRanges, client-rasterized full-frame PNG burn-ins for text
overlays / lower thirds / brand, optional 9:16 shorts crop, and metadata when a
requested overlay could not be drawn.
"""

from __future__ import annotations

import base64
import binascii
import logging
import json
import math
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.db.models import AiResult, GeneratedMedia, Project, Video, VideoTranscription
from app.services.mask_matte import render_matte_video
from app.services.color_adjust_keyframes import build_keyframed_adjust_filter_chain
from app.jobs.rough_cut_effect import _resolve_media_source
from app.utils.cloudinary import cloudinary_credentials_configured, upload_local_path_to_cloudinary
from app.services.product_analytics import emit_once

_XFADE_TRANSITIONS = {
    "fade", "wipeleft", "wiperight", "wipeup", "wipedown", "slideleft",
    "slideright", "slideup", "slidedown", "circlecrop", "rectcrop",
    "distance", "fadeblack", "fadewhite", "radial", "smoothleft",
    "smoothright", "smoothup", "smoothdown", "circleopen", "circleclose",
    "vertopen", "vertclose", "horzopen", "horzclose", "dissolve",
    "pixelize", "diagtl", "diagtr", "diagbl", "diagbr", "hlslice",
    "hrslice", "vuslice", "vdslice", "hblur", "fadegrays", "wipetl",
    "wipetr", "wipebl", "wipebr", "squeezeh", "squeezev", "zoomin",
    "fadefast", "fadeslow", "hlwind", "hrwind", "vuwind", "vdwind",
    "coverleft", "coverright", "coverup", "coverdown", "revealleft",
    "revealright", "revealup", "revealdown",
}


def _record_export_feature_result_use(
    db: Session,
    *,
    row: AiResult,
    video: Video | None,
    feature_keys: set[str],
) -> None:
    if video is None or not feature_keys:
        return
    project = db.query(Project).filter(Project.id == video.project_id).first()
    for feature_key in sorted(feature_keys):
        emit_once(
            db,
            "feature_result_used",
            event_id=f"rough-cut-export:{row.id}:result-used:{feature_key}",
            user_id=video.uploader_id,
            workspace_id=project.workspace_id if project else None,
            source="worker",
            properties={
                "feature_key": feature_key,
                "project_id": video.project_id,
                "video_id": video.id,
                "job_id": str(row.id),
                "job_type": "rough_cut_export_job",
                "result_action": "included_in_export",
                "result": "success",
            },
        )


def _range_index(value: object, ranges: list[tuple[float, float]]) -> int | None:
    if not isinstance(value, dict):
        return None
    try:
        start = float(value.get("start"))
        end = float(value.get("end"))
    except (TypeError, ValueError):
        return None
    for index, (candidate_start, candidate_end) in enumerate(ranges):
        if abs(candidate_start - start) <= 0.015 and abs(candidate_end - end) <= 0.015:
            return index
    return None


def _normalize_transitions(value: object, ranges: list[tuple[float, float]]) -> list[dict[str, object]]:
    """Validate transition payloads and clamp them against their clip pair.

    A malformed transition is an omitted decoration, never a failed export.
    Source ranges outrank indices because the worker normalizes source order.
    """
    if not isinstance(value, list) or not ranges:
        return []
    output: list[dict[str, object]] = []
    seen: set[tuple[str, int, int]] = set()
    for raw in value[:128]:
        if not isinstance(raw, dict):
            continue
        placement = str(raw.get("placement") or "")
        if placement not in {"between", "in", "out"}:
            continue
        left_index = _range_index(raw.get("leftRange"), ranges)
        right_index = _range_index(raw.get("rightRange"), ranges)
        if left_index is None:
            try:
                candidate = int(raw.get("leftIndex"))
                left_index = candidate if 0 <= candidate < len(ranges) else None
            except (TypeError, ValueError):
                left_index = None
        if right_index is None:
            try:
                candidate = int(raw.get("rightIndex"))
                right_index = candidate if 0 <= candidate < len(ranges) else None
            except (TypeError, ValueError):
                right_index = None
        if placement == "between" and (left_index is None or right_index != left_index + 1):
            continue
        if placement == "in" and right_index is None:
            continue
        if placement == "out" and left_index is None:
            continue
        alignment = str(raw.get("alignment") or "center")
        if alignment not in {"start", "center", "end"}:
            alignment = "center"
        left_duration = ranges[left_index][1] - ranges[left_index][0] if left_index is not None else float("inf")
        right_duration = ranges[right_index][1] - ranges[right_index][0] if right_index is not None else float("inf")
        if placement == "in":
            available = right_duration
        elif placement == "out":
            available = left_duration
        elif alignment == "start":
            available = right_duration
        elif alignment == "end":
            available = left_duration
        else:
            available = min(left_duration, right_duration) * 2
        maximum = min(5.0, available)
        if maximum < 0.1:
            continue
        try:
            duration = float(raw.get("duration", 0.6))
        except (TypeError, ValueError):
            duration = 0.6
        if not math.isfinite(duration):
            duration = 0.6
        duration = max(0.1, min(maximum, duration))
        preset = str(raw.get("exportPreset") or "dissolve").lower()
        if preset not in _XFADE_TRANSITIONS:
            preset = "dissolve"
        key = (placement, -1 if left_index is None else left_index, -1 if right_index is None else right_index)
        if key in seen:
            continue
        seen.add(key)
        output.append({
            "id": str(raw.get("id") or f"transition-{len(output)}"),
            "presetId": str(raw.get("presetId") or "cross-dissolve"),
            "preset": preset,
            "placement": placement,
            "leftIndex": left_index,
            "rightIndex": right_index,
            "duration": duration,
            "alignment": alignment,
        })
    return output


def _transition_video_command(
    parts: list[Path],
    durations: list[float],
    transitions: list[dict[str, object]],
    *,
    audio_path: Path,
    crf: int,
    output: Path,
) -> list[str] | None:
    """Build a duration-preserving transition graph.

    Each connected clip pair receives half-duration cloned handles. Xfade then
    consumes those handles, so the output remains exactly sum(durations) instead
    of getting shorter once per transition. Unconnected groups hard-cut via
    concat. Audio is muxed from the already length-correct segment concat; its
    clip-edge fades are applied while each WAV part is built.
    """
    if not parts or not transitions:
        return None
    between_by_left = {
        int(item["leftIndex"]): item
        for item in transitions
        if item.get("placement") == "between" and isinstance(item.get("leftIndex"), int)
    }
    edge_in = {int(item["rightIndex"]): item for item in transitions if item.get("placement") == "in" and isinstance(item.get("rightIndex"), int)}
    edge_out = {int(item["leftIndex"]): item for item in transitions if item.get("placement") == "out" and isinstance(item.get("leftIndex"), int)}
    command = ["ffmpeg", "-y"]
    for part in parts:
        command.extend(["-i", str(part)])
    command.extend(["-i", str(audio_path)])
    filters: list[str] = []
    groups: list[str] = []
    group_start = 0
    group_index = 0
    while group_start < len(parts):
        group_end = group_start
        while group_end in between_by_left and group_end + 1 < len(parts):
            group_end += 1
        labels: list[str] = []
        for index in range(group_start, group_end + 1):
            previous = between_by_left.get(index - 1)
            following = between_by_left.get(index)
            head = 0.0
            if previous:
                previous_duration = float(previous["duration"])
                previous_alignment = previous.get("alignment", "center")
                head = previous_duration if previous_alignment == "end" else previous_duration / 2 if previous_alignment == "center" else 0.0
            tail = 0.0
            if following:
                following_duration = float(following["duration"])
                following_alignment = following.get("alignment", "center")
                tail = following_duration if following_alignment == "start" else following_duration / 2 if following_alignment == "center" else 0.0
            chain = f"[{index}:v]settb=AVTB,format=yuv420p"
            if head > 0:
                chain += f",tpad=start_mode=clone:start_duration={head:.6f}"
            if tail > 0:
                chain += f",tpad=stop_mode=clone:stop_duration={tail:.6f}"
            if index in edge_in:
                fade = min(durations[index], float(edge_in[index]["duration"]))
                chain += f",fade=t=in:st=0:d={fade:.6f}:color=black"
            if index in edge_out:
                fade = min(durations[index], float(edge_out[index]["duration"]))
                chain += f",fade=t=out:st={max(0.0, durations[index] - fade):.6f}:d={fade:.6f}:color=black"
            label = f"tv{index}"
            filters.append(f"{chain}[{label}]")
            labels.append(label)
        current = labels[0]
        elapsed = durations[group_start]
        for local, left_index in enumerate(range(group_start, group_end)):
            item = between_by_left[left_index]
            transition_duration = float(item["duration"])
            alignment = item.get("alignment", "center")
            incoming_handle = transition_duration if alignment == "end" else transition_duration / 2 if alignment == "center" else 0.0
            next_label = labels[local + 1]
            out_label = f"tx{group_index}_{local}"
            offset = max(0.0, elapsed - incoming_handle)
            filters.append(
                f"[{current}][{next_label}]xfade=transition={item['preset']}:duration={transition_duration:.6f}:offset={offset:.6f}[{out_label}]"
            )
            current = out_label
            elapsed += durations[left_index + 1]
        groups.append(current)
        group_start = group_end + 1
        group_index += 1
    if len(groups) > 1:
        concat_inputs = "".join(f"[{label}]" for label in groups)
        filters.append(f"{concat_inputs}concat=n={len(groups)}:v=1:a=0[tvout]")
        video_label = "tvout"
    else:
        video_label = groups[0]
    command.extend([
        "-filter_complex", ";".join(filters),
        "-map", f"[{video_label}]",
        "-map", f"{len(parts)}:a:0",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", "-shortest", str(output),
    ])
    return command


def _transition_audio_settings(
    base: dict[str, Any] | None,
    index: int,
    transitions: list[dict[str, object]],
    *,
    fold_between: bool = True,
) -> dict[str, Any] | None:
    """Fold linked transition ramps into the segment's existing audio edit.

    `fold_between=False` skips the between-cut halves: when the merge step
    runs a real `acrossfade` over those boundaries, folding them here too
    would fade the same seconds twice (a dip inside the crossfade). Edge
    in/out ramps always fold — they have no crossfade counterpart.
    """
    result = dict(base or {})
    fade_in = float(result.get("fadeIn") or 0.0)
    fade_out = float(result.get("fadeOut") or 0.0)
    for item in transitions:
        duration = float(item.get("duration") or 0.0)
        placement = item.get("placement")
        if placement == "between":
            if not fold_between:
                continue
            if item.get("rightIndex") == index:
                fade_in = max(fade_in, duration / 2)
            if item.get("leftIndex") == index:
                fade_out = max(fade_out, duration / 2)
        elif placement == "in" and item.get("rightIndex") == index:
            fade_in = max(fade_in, duration)
        elif placement == "out" and item.get("leftIndex") == index:
            fade_out = max(fade_out, duration)
    if fade_in > 0:
        result["fadeIn"] = fade_in
    if fade_out > 0:
        result["fadeOut"] = fade_out
    return result or None


def _crossfade_boundaries(
    transitions: list[dict[str, object]], segment_count: int
) -> dict[int, float]:
    """Between-cut boundaries that get a real audio crossfade.

    Keyed by the LEFT segment index. Only adjacent pairs qualify — the video
    side xfades exactly those; a non-adjacent "between" is already a hard cut
    there and stays one here.
    """
    out: dict[int, float] = {}
    for item in transitions:
        if item.get("placement") != "between":
            continue
        try:
            left = int(item.get("leftIndex"))  # type: ignore[arg-type]
            right = int(item.get("rightIndex"))  # type: ignore[arg-type]
            duration = float(item.get("duration") or 0.0)
        except (TypeError, ValueError):
            continue
        if right != left + 1 or not (0 <= left < segment_count - 1):
            continue
        if duration > 0.03:
            out[left] = min(duration, 5.0)
    return out


def _crossfade_wav_command(
    wav_parts: list[Path], boundaries: dict[int, float], output: Path
) -> list[str] | None:
    """Merge segment WAVs with real `acrossfade`s where transitions sit.

    Runtime is preserved the same way the video side preserves it with cloned
    handles: each crossfaded boundary pads the left segment's tail and leads
    the right segment in with half the duration of silence, so the overlap
    consumes exactly the seconds the padding added and the audio stays in
    sync with the picture. The old behaviour — per-segment fades meeting at a
    hard cut — dipped every dissolve to silence (plan §5.3).
    """
    if not boundaries or len(wav_parts) < 2:
        return None
    graph: list[str] = []
    for index in range(len(wav_parts)):
        chain: list[str] = ["aformat=sample_rates=48000:channel_layouts=stereo"]
        left_cross = boundaries.get(index - 1)
        right_cross = boundaries.get(index)
        if left_cross:
            lead_ms = int(round(left_cross / 2 * 1000))
            chain.append(f"adelay=delays={lead_ms}:all=1")
        if right_cross:
            chain.append(f"apad=pad_dur={right_cross / 2:.3f}")
        graph.append(f"[{index}:a]{','.join(chain)}[p{index}]")

    current = "[p0]"
    for index in range(1, len(wav_parts)):
        merged = f"[m{index}]"
        cross = boundaries.get(index - 1)
        if cross:
            graph.append(
                f"{current}[p{index}]acrossfade=d={cross:.3f}:c1=tri:c2=tri{merged}"
            )
        else:
            graph.append(f"{current}[p{index}]concat=n=2:v=0:a=1{merged}")
        current = merged

    inputs: list[str] = []
    for part in wav_parts:
        inputs += ["-i", str(part)]
    return [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", ";".join(graph),
        "-map", current,
        "-acodec", "pcm_s16le", "-ar", "48000", "-ac", "2",
        str(output),
    ]
logger = logging.getLogger(__name__)

# Absolute bound on any timeline position. The old bound was the primary
# media's own duration, which is no longer the end of the timeline -- clips
# appended after the A-roll sit past it -- so this is what stops a malformed
# `end: 1e9` from pinning ffmpeg instead.
MAX_TIMELINE_SECONDS = 24 * 60 * 60.0
# How much timeline may be appended after the primary media runs out. Generous
# for real edits, and a hard stop on a draft that would otherwise ask the
# worker to render black for a day.
MAX_TAIL_SECONDS = 4 * 60 * 60.0
# Client-rasterized burn-in frames (text overlays / lower thirds / brand). The
# browser sends full-frame transparent PNGs, so these are real image bytes on
# a JSON body -- both the count and each frame are bounded before anything is
# written to disk or handed to ffmpeg. The route applies the same caps so the
# oversized bytes never reach the AiResult row either.
MAX_BURN_INS = 32
MAX_BURN_IN_PNG_BYTES = 6 * 1024 * 1024
# The 8-byte PNG signature. A base64 blob that decodes cleanly is still not
# necessarily a PNG, and ffmpeg is not the right place to find that out.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# Shorter than this and the overlay is a flicker, not a title.
MIN_BURN_IN_SECONDS = 0.02


def _merge_result_payload(existing: dict | None, patch: dict) -> dict:
    base = dict(existing or {})
    base.update(patch)
    return base


def _ffprobe_has_video(input_src: str) -> bool:
    try:
        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                input_src,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        return bool(out.stdout.strip())
    except Exception:  # noqa: BLE001
        return False


def _ffprobe_has_audio(input_src: str) -> bool:
    """Whether a source carries an audio stream.

    Needed before any filter graph references `[N:a]`: ffmpeg fails the whole
    command on a missing stream label, so a silent B-roll clip would take the
    entire export down rather than simply contributing no sound.
    """
    try:
        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                input_src,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        return bool(out.stdout.strip())
    except Exception:  # noqa: BLE001
        return False


def _ffprobe_avg_frame_rate(input_src: str) -> str | None:
    """Return e.g. '30/1' or '30000/1001', or None."""
    try:
        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=avg_frame_rate",
                "-of",
                "csv=p=0",
                input_src,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        line = (out.stdout or "").strip().splitlines()
        return line[0].strip() if line else None
    except Exception:  # noqa: BLE001
        return None


def _ffprobe_video_size(input_src: str) -> tuple[int, int] | None:
    """Return the first video stream's (width, height), or None."""
    try:
        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0:s=x",
                input_src,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        line = (out.stdout or "").strip().splitlines()
        if not line:
            return None
        width, _, height = line[0].strip().partition("x")
        w, h = int(width), int(height)
        return (w, h) if w > 0 and h > 0 else None
    except Exception:  # noqa: BLE001
        return None


def _run_ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(args, capture_output=True, text=True, timeout=7200)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "")[-8000:]
        logger.error("ffmpeg failed (rc=%s): %s", proc.returncode, err)
        raise RuntimeError(err or "ffmpeg failed")


def _ffprobe_duration(input_src: str) -> float | None:
    try:
        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                input_src,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        value = (out.stdout or "").strip().splitlines()
        if not value:
            return None
        duration = float(value[0].strip())
        return duration if duration > 0 else None
    except Exception:  # noqa: BLE001
        return None


def _normalize_ranges(raw: object, max_end: float | None = None) -> list[tuple[float, float]]:
    """`max_end`, when given (the probed source duration -- I8), clamps
    every range's end so a hostile/malformed keep-range (e.g. `end: 1e9`)
    can't pin the matte renderer or ffmpeg rasterising far past the real
    media."""
    if not isinstance(raw, list):
        return []
    out: list[tuple[float, float]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item.get("start", 0))
            end = float(item.get("end", 0))
        except (TypeError, ValueError):
            continue
        if max_end is not None:
            start = min(start, max_end)
            end = min(end, max_end)
        if end - start > 0.05:
            out.append((start, end))
    # Deliberately NOT sorted: the client sends keep ranges in play order, and
    # a reordered timeline must render reordered. Sorting by source start here
    # silently destroyed every reorder the editor made (plan §5.3).
    return out


def _masked_filter_complex(
    vf: str, scale_w: int, scale_h: int, *, matte_input: int = 1,
    background_color: str = "black",
) -> str:
    """Builds the `-filter_complex` for a masked export segment.

    Trap: `scale`/`pad` (baked into `vf`) genuinely take colon-separated
    "W:H" ("1920:1080"); ffmpeg's `color` source does NOT -- it wants
    "WxH" ("1920x1080") and silently mis-parses the colon form
    ("No option name near '1080'"), which used to fail every masked
    export outright (this is not caught by the matte fail-open guard --
    that only wraps `render_matte_video`, not this ffmpeg invocation).
    Pass `color` its size as `f"{scale_w}x{scale_h}"`, built from the
    already-int scale_w/scale_h rather than string-munging the colon-form
    `scale` string, so the two forms cannot drift back together.
    """
    color_size = f"{scale_w}x{scale_h}"
    return (
        f"[0:v]{vf},split[base][alpha_source];"
        f"[alpha_source]alphaextract[source_alpha];"
        f"[{matte_input}:v]format=gray[mask_alpha];"
        f"[source_alpha][mask_alpha]blend=all_mode=multiply[combined_alpha];"
        f"[base][combined_alpha]alphamerge[m];"
        f"color={background_color}:s={color_size}[bg];"
        f"[bg][m]overlay=shortest=1[v]"
    )


def _approved_processed_ranges(
    db: Session,
    video_id: int,
    requested: object,
) -> dict[tuple[float, float], str]:
    """Return requested processed visuals backed by completed owned jobs.

    `processedRanges` crosses a trust boundary. Resolving a browser-provided URL
    directly would turn the worker into an SSRF/local-file reader. A source is
    usable only when its URL, effect type, and range exactly match a completed
    Remove BG or Retouch row for this video.
    """
    if not isinstance(requested, list):
        return {}

    approved: set[tuple[float, float, str, str]] = set()
    rows = (
        db.query(AiResult)
        .filter(
            AiResult.video_id == video_id,
            AiResult.result_type == "rough_cut_effect",
            AiResult.status == "completed",
        )
        .all()
    )
    for row in rows:
        data = row.result_data if isinstance(row.result_data, dict) else {}
        effect_type = str(data.get("effectType") or "")
        if effect_type not in {"remove_bg", "retouch"}:
            continue
        target = data.get("clipTarget") if isinstance(data.get("clipTarget"), dict) else {}
        url = data.get("outputUrl")
        try:
            start = round(float(target.get("start", 0)), 3)
            end = round(float(target.get("end", 0)), 3)
        except (TypeError, ValueError):
            continue
        if isinstance(url, str) and url.strip() and end > start:
            approved.add((start, end, url.strip(), effect_type))

    result: dict[tuple[float, float], str] = {}
    for item in requested:
        if not isinstance(item, dict):
            continue
        url = item.get("sourceUrl")
        effect_type = str(item.get("effectType") or "remove_bg")
        try:
            start = round(float(item.get("start", 0)), 3)
            end = round(float(item.get("end", 0)), 3)
        except (TypeError, ValueError):
            continue
        if isinstance(url, str) and (start, end, url.strip(), effect_type) in approved:
            result[(start, end)] = _resolve_media_source(url.strip())
    return result


def _normalize_timeline_layers(
    requested: object,
    *,
    video_id: int,
) -> list[dict[str, Any]]:
    """Sanitize first-class picture clips before they reach FFmpeg.

    Timeline layer URLs are intentionally not accepted. Every source is
    resolved by id from a row the caller already owns -- the video being
    exported, another video in the same project, or a completed effect on one
    of them -- so a draft can never turn export into an arbitrary URL fetcher.
    Authorization of those ids happens in `_approved_timeline_layers`; this
    function only sanitizes shape and bounds.

    `start`/`end` are TIMELINE seconds and are deliberately not clamped to the
    primary media's duration: clips appended after the A-roll legitimately sit
    past it (the editor calls that region the tail). `sourceStart` is a time in
    the layer's OWN media, so it can only be bounded once that media has been
    resolved -- see `_approved_timeline_layers`.
    """
    if not isinstance(requested, list):
        return []
    result: list[dict[str, Any]] = []
    for raw in requested[:64]:
        if not isinstance(raw, dict):
            continue
        # Stills are first-class picture clips. Dropping them here is what made
        # AI-generated B-roll images vanish from the render while still showing
        # in the editor preview. "audio" is a clip on an audio lane -- music or
        # an SFX hit -- which has no picture at all and is mixed rather than
        # composited; dropping those is what made them play in the editor and
        # be silent in every export.
        layer_kind = str(raw.get("kind") or "video")
        if layer_kind not in {"video", "image", "audio"}:
            continue
        # A layer without an explicit id came from the primary media; every
        # draft written before B-roll from other clips existed looks like this.
        layer_video_id = int(video_id)
        if raw.get("videoId") is not None:
            try:
                layer_video_id = int(raw["videoId"])
            except (TypeError, ValueError):
                continue

        # Which table the clip is cut from. `videoId` alone cannot say: media
        # generated by the AI lives in `generated_media`, and reading its absent
        # `videoId` as "the primary video" is exactly how B-roll used to render
        # as duplicated A-roll.
        source_kind = str(raw.get("sourceKind") or "")
        source_id: int | None = None
        if raw.get("sourceId") is not None:
            try:
                source_id = int(raw["sourceId"])
            except (TypeError, ValueError):
                source_id = None
        if source_kind not in {"video", "generated"}:
            # Legacy shape: no discriminator at all means a `videos` row, which
            # is what every layer was before generated media could be placed.
            source_kind, source_id = "video", layer_video_id
        elif source_id is None:
            if source_kind != "video":
                continue
            source_id = layer_video_id

        try:
            start = float(raw.get("start", 0))
            end = float(raw.get("end", 0))
            source_start = float(raw.get("sourceStart", 0))
            track_order = int(raw.get("trackOrder", 0))
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in (start, end, source_start)):
            continue
        start = max(0.0, min(MAX_TIMELINE_SECONDS, start))
        source_start = max(0.0, min(MAX_TIMELINE_SECONDS, source_start))
        end = max(start, min(MAX_TIMELINE_SECONDS, end))
        if end - start <= 0.05:
            continue
        clip_id = str(raw.get("id") or "")[:160]
        clip_key = str(raw.get("clipKey") or f"media:{clip_id}")[:200]
        if not clip_id or not clip_key.startswith("media:"):
            continue
        processed_result_id: int | None = None
        try:
            if raw.get("processedResultId") is not None:
                processed_result_id = int(raw["processedResultId"])
        except (TypeError, ValueError):
            processed_result_id = None
        effect_type = str(raw.get("processedEffectType") or "")
        if effect_type not in {"remove_bg", "retouch"}:
            effect_type = ""
        processed_audio_result_id: int | None = None
        try:
            if raw.get("processedAudioResultId") is not None:
                processed_audio_result_id = int(raw["processedAudioResultId"])
        except (TypeError, ValueError):
            processed_audio_result_id = None
        settings = raw.get("settings") if isinstance(raw.get("settings"), dict) else {}
        result.append(
            {
                "id": clip_id,
                "clipKey": clip_key,
                "kind": layer_kind,
                "videoId": layer_video_id,
                "sourceKind": source_kind,
                "sourceId": int(source_id),
                # The composite key every later stage resolves against. A bare
                # video id is not unique once two tables can supply a clip.
                "sourceRef": (source_kind, int(source_id)),
                "start": start,
                "end": end,
                "sourceStart": source_start,
                "trackOrder": max(0, min(999, track_order)),
                "aboveText": raw.get("aboveText") is not False,
                # B-roll ships muted; a clip chosen for what is said in it does
                # not. The editor decides, and the render has to agree -- an
                # appended interview answer that exports silent is not exported.
                # An audio-lane clip is the other way round: sound is the only
                # thing it has, so it is audible unless the payload says other-
                # wise, and an older client that omits the flag still gets it.
                "audioEnabled": (
                    raw.get("audioEnabled") is not False
                    if layer_kind == "audio"
                    else raw.get("audioEnabled") is True
                ),
                "processedResultId": processed_result_id,
                "processedEffectType": effect_type,
                "processedAudioResultId": processed_audio_result_id,
                "settings": {
                    key: value
                    for key, value in settings.items()
                    if key in {"video", "adjust", "animation", "keyframes", "mirror", "rotation", "audio"}
                },
            }
        )
    return result


def _authorized_layer_sources(
    db: Session,
    video_id: int,
    layers: list[dict[str, Any]],
    *,
    media_src: str,
    source_duration: float | None,
) -> dict[tuple[str, int], dict[str, Any]]:
    """Which media these layers may actually be cut from, keyed by source ref.

    The video being exported is always allowed. Anything else has to belong to
    the same project -- either another video, or a row in `generated_media`,
    which together are exactly the set the editor's media panel offers -- and is
    looked up here rather than trusted from the payload, so an id the requester
    has no claim on simply resolves to nothing and its layers are dropped. The
    client sends an id, never a path; that is what keeps export from becoming an
    arbitrary URL fetcher.

    Each entry carries the media's own duration and whether it has sound, both
    of which the primary's values cannot stand in for once the source differs.
    A still has neither, and says so: `duration` is None (it plays for as long
    as the clip asks) and `hasAudio` is False.
    """
    sources: dict[tuple[str, int], dict[str, Any]] = {}
    if media_src.strip():
        sources[("video", int(video_id))] = {
            "path": media_src,
            # The caller already paid for this probe; on a remote source it is
            # not a cheap call to repeat.
            "duration": source_duration,
            "hasAudio": _ffprobe_has_audio(media_src),
            "isStill": False,
        }

    wanted = {layer["sourceRef"] for layer in layers} - set(sources)
    if not wanted:
        return sources

    primary = db.query(Video).filter(Video.id == video_id).first()
    project_id = getattr(primary, "project_id", None)
    if project_id is None:
        return sources

    video_ids = {ref[1] for ref in wanted if ref[0] == "video"}
    if video_ids:
        rows = (
            db.query(Video)
            .filter(Video.id.in_(video_ids), Video.project_id == project_id)
            .all()
        )
        for row in rows:
            path = (row.file_path or "").strip()
            if not path:
                continue
            try:
                resolved = _resolve_media_source(path)
            except Exception:  # noqa: BLE001
                logger.exception("Unable to resolve timeline layer source video %s", row.id)
                continue
            sources[("video", int(row.id))] = {
                "path": resolved,
                "duration": _ffprobe_duration(resolved),
                "hasAudio": _ffprobe_has_audio(resolved),
                "isStill": False,
            }

    generated_ids = {ref[1] for ref in wanted if ref[0] == "generated"}
    if generated_ids:
        rows = (
            db.query(GeneratedMedia)
            .filter(
                GeneratedMedia.id.in_(generated_ids),
                # Scoped to the project, matching the media panel. A generation
                # from someone else's project is not a source here.
                GeneratedMedia.project_id == project_id,
                # An unfinished or failed generation has no bytes to composite.
                GeneratedMedia.status == "ready",
            )
            .all()
        )
        for row in rows:
            url = (row.url or "").strip()
            if not url:
                continue
            try:
                resolved = _resolve_media_source(url)
            except Exception:  # noqa: BLE001
                logger.exception("Unable to resolve generated media %s", row.id)
                continue
            is_still = str(row.kind or "") != "video"
            sources[("generated", int(row.id))] = {
                "path": resolved,
                # A still has no intrinsic length, so nothing may bound the
                # clip against it -- see the trim in `_approved_timeline_layers`.
                "duration": None if is_still else _ffprobe_duration(resolved),
                "hasAudio": False if is_still else _ffprobe_has_audio(resolved),
                "isStill": is_still,
            }
    return sources


def _approved_timeline_layers(
    db: Session,
    video_id: int,
    requested: object,
    *,
    media_src: str,
    source_duration: float | None,
) -> list[dict[str, Any]]:
    """Resolve timeline layers only from the owned source/effect records.

    A layer may name a video other than the one being exported -- that is what
    B-roll and transcript-selected clips are -- so the set of usable sources is
    every video in the same project. That is exactly the set the editor's media
    panel offers, and it is resolved here from the database by id: the client
    sends an id, never a path.
    """
    layers = _normalize_timeline_layers(requested, video_id=video_id)
    if not layers:
        return []

    sources = _authorized_layer_sources(
        db, video_id, layers, media_src=media_src, source_duration=source_duration
    )
    layers = [layer for layer in layers if layer["sourceRef"] in sources]
    if not layers:
        return []

    # Bound each layer against its OWN media: a clip cannot play more than the
    # source it comes from has, and until now "the source" was always the
    # primary, which is wrong the moment a layer comes from elsewhere.
    for layer in layers:
        source = sources[layer["sourceRef"]]
        layer["source"] = source["path"]
        layer["processed"] = False
        layer["hasAudio"] = bool(source["hasAudio"])
        # "Still" only ever means "one frame, loop it for the clip's length",
        # which is a statement about picture. An audio lane has none, so it is
        # never looped as one -- it is seeked and trimmed like any other sound.
        layer["isStill"] = bool(source.get("isStill")) and layer["kind"] != "audio"
        own_duration = source["duration"]
        # A still is not bounded by a duration it does not have: it plays for
        # exactly as long as the clip asks, which is the whole point of placing
        # one. Trimming it against a probe would collapse it to nothing.
        if own_duration and not layer["isStill"]:
            layer["sourceStart"] = max(0.0, min(own_duration, float(layer["sourceStart"])))
            available = max(0.0, own_duration - float(layer["sourceStart"]))
            layer["end"] = max(float(layer["start"]), min(float(layer["end"]), float(layer["start"]) + available))
    layers = [layer for layer in layers if float(layer["end"]) - float(layer["start"]) > 0.05]

    # Effects are keyed by `video_id`, so only a layer cut from a `videos` row
    # can carry one. A generated clip has no effect rows to claim.
    requested_ids = {
        int(item["processedResultId"])
        for item in layers
        if isinstance(item.get("processedResultId"), int) and item["sourceKind"] == "video"
    }
    requested_ids.update(
        int(item["processedAudioResultId"])
        for item in layers
        if isinstance(item.get("processedAudioResultId"), int) and item["sourceKind"] == "video"
    )
    approved_video_ids = {ref[1] for ref in sources if ref[0] == "video"}
    approved_rows: dict[int, AiResult] = {}
    if requested_ids:
        rows = (
            db.query(AiResult)
            .filter(
                AiResult.id.in_(requested_ids),
                # An effect belongs to the clip it was run on, which for a
                # foreign layer is not the video being exported.
                AiResult.video_id.in_(approved_video_ids),
                AiResult.result_type == "rough_cut_effect",
                AiResult.status == "completed",
            )
            .all()
        )
        approved_rows = {int(row.id): row for row in rows}

    for layer in layers:
        if layer["sourceKind"] != "video":
            continue
        result_id = layer.get("processedResultId")
        row = approved_rows.get(result_id) if isinstance(result_id, int) else None
        if row is None:
            continue
        data = row.result_data if isinstance(row.result_data, dict) else {}
        effect_type = str(data.get("effectType") or "")
        output_url = data.get("outputUrl")
        target = data.get("clipTarget") if isinstance(data.get("clipTarget"), dict) else {}
        timing_matches = False
        try:
            target_start = round(float(target.get("start", -1)), 3)
            target_end = round(float(target.get("end", -1)), 3)
            layer_start = round(float(layer["sourceStart"]), 3)
            layer_end = round(
                float(layer["sourceStart"]) + float(layer["end"]) - float(layer["start"]),
                3,
            )
            timing_matches = target_start == layer_start and target_end == layer_end
        except (TypeError, ValueError):
            timing_matches = False
        if (
            # An effect rendered on a different clip is a different picture.
            int(getattr(row, "video_id", 0) or 0) != int(layer["videoId"])
            or (data.get("clipKey") != layer["clipKey"] and not timing_matches)
            or effect_type != layer.get("processedEffectType")
            or effect_type not in {"remove_bg", "retouch"}
            or not isinstance(output_url, str)
            or not output_url.strip()
        ):
            continue
        try:
            layer["source"] = _resolve_media_source(output_url.strip())
            layer["processed"] = True
        except Exception:  # noqa: BLE001
            logger.exception("Unable to resolve approved timeline effect %s", result_id)

    # An audio-lane clip has no picture, so its completed enhancement can
    # safely replace the whole input. Picture layers need separate visual/audio
    # inputs and keep their original source here.
    for layer in layers:
        if layer["sourceKind"] != "video" or layer["kind"] != "audio":
            continue
        result_id = layer.get("processedAudioResultId")
        row = approved_rows.get(result_id) if isinstance(result_id, int) else None
        if row is None:
            continue
        data = row.result_data if isinstance(row.result_data, dict) else {}
        target = data.get("clipTarget") if isinstance(data.get("clipTarget"), dict) else {}
        output_url = data.get("outputUrl")
        try:
            target_start = round(float(target.get("start", -1)), 3)
            target_end = round(float(target.get("end", -1)), 3)
            layer_start = round(float(layer["sourceStart"]), 3)
            layer_end = round(
                float(layer["sourceStart"]) + float(layer["end"]) - float(layer["start"]),
                3,
            )
        except (TypeError, ValueError):
            continue
        if (
            int(getattr(row, "video_id", 0) or 0) != int(layer["videoId"])
            or data.get("effectType") != "audio"
            or (data.get("clipKey") != layer["clipKey"] and (target_start != layer_start or target_end != layer_end))
            or not isinstance(output_url, str)
            or not output_url.strip()
        ):
            continue
        try:
            layer["source"] = _resolve_media_source(output_url.strip())
            layer["processed"] = True
            layer["hasAudio"] = True
        except Exception:  # noqa: BLE001
            logger.exception("Unable to resolve approved timeline audio effect %s", result_id)
    return layers


def _remap_timeline_layers_to_export(
    layers: list[dict[str, Any]],
    kept_ranges: list[tuple[float, float]],
    *,
    source_duration: float | None = None,
    rates: list[float] | None = None,
) -> list[dict[str, Any]]:
    """Intersect source-time layers and map them onto the concatenated MP4.

    Inside the primary media a layer is placed by intersecting it with what
    survived the cuts, so it ripples along with them. Past the primary media
    there are no cuts to ripple through -- the tail is content the source never
    had -- so it maps one-for-one after the kept total. That is the same
    mapping the editor draws with, and the two have to agree or an appended
    clip lands somewhere other than where the user put it.

    Every chunk carries the span of the LAYER it was cut from
    (`layerDuration`) and where the chunk sits inside it (`layerOffset`).
    A cut can split one clip into several chunks, and anything that runs over
    the clip as a whole -- a fade in particular -- has to be measured against
    the clip, not against whichever fragment it landed in.
    """
    chunks: list[dict[str, Any]] = []
    output_cursor = 0.0
    for range_index, (kept_start, kept_end) in enumerate(kept_ranges):
        rate = rates[range_index] if rates and range_index < len(rates) else 1.0
        for layer in layers:
            overlap_start = max(kept_start, float(layer["start"]))
            overlap_end = min(kept_end, float(layer["end"]))
            if overlap_end - overlap_start <= 0.02:
                continue
            clip_offset = overlap_start - float(layer["start"])
            chunks.append(
                {
                    **layer,
                    "outputStart": output_cursor + (overlap_start - kept_start) / rate,
                    "outputEnd": output_cursor + (overlap_end - kept_start) / rate,
                    "clipOffset": clip_offset,
                    "layerDuration": float(layer["end"]) - float(layer["start"]),
                    "layerOffset": clip_offset,
                    "sourceSeek": (
                        clip_offset
                        if bool(layer.get("processed"))
                        else float(layer["sourceStart"]) + clip_offset
                    ),
                }
            )
        output_cursor += (kept_end - kept_start) / rate

    if source_duration and source_duration > 0:
        kept_total = output_cursor
        for layer in layers:
            tail_start = max(float(layer["start"]), source_duration)
            tail_end = float(layer["end"])
            if tail_end - tail_start <= 0.02:
                continue
            clip_offset = tail_start - float(layer["start"])
            chunks.append(
                {
                    **layer,
                    "outputStart": kept_total + tail_start - source_duration,
                    "outputEnd": kept_total + tail_end - source_duration,
                    "clipOffset": clip_offset,
                    "layerDuration": float(layer["end"]) - float(layer["start"]),
                    "layerOffset": clip_offset,
                    "sourceSeek": (
                        clip_offset
                        if bool(layer.get("processed"))
                        else float(layer["sourceStart"]) + clip_offset
                    ),
                }
            )
    # Higher track order is visually lower. Render the bottom chunks first so
    # track order zero is the last (topmost) input, matching the viewer.
    chunks.sort(
        key=lambda item: (
            -int(item["trackOrder"]),
            float(item["outputStart"]),
            str(item["id"]),
        )
    )
    return chunks[:160]


def _range_settings(raw: object) -> dict[tuple[float, float], dict[str, Any]]:
    """Map an export payload's exact source ranges to plain settings objects."""
    if not isinstance(raw, list):
        return {}
    result: dict[tuple[float, float], dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("settings"), dict):
            continue
        try:
            start = round(float(item.get("start", 0)), 3)
            end = round(float(item.get("end", 0)), 3)
        except (TypeError, ValueError):
            continue
        if end > start:
            result[(start, end)] = dict(item["settings"])
    return result


def _audio_range_settings(raw: object) -> dict[tuple[float, float], dict[str, Any]]:
    """Map exact source ranges to flat per-clip audio settings.

    `audioRanges` items are flat ({start, end, volume?, fadeIn?, fadeOut?})
    rather than `settings`-wrapped, so `_range_settings` cannot ingest them.
    Keys round the same way so lookups stay aligned with keepRanges.
    """
    if not isinstance(raw, list):
        return {}
    result: dict[tuple[float, float], dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            start = round(float(item.get("start", 0)), 3)
            end = round(float(item.get("end", 0)), 3)
        except (TypeError, ValueError):
            continue
        if end > start:
            settings = {key: item.get(key) for key in ("volume", "fadeIn", "fadeOut")}
            if item.get("enhancedResultId") is not None:
                try:
                    settings["enhancedResultId"] = int(item["enhancedResultId"])
                except (TypeError, ValueError):
                    pass
            result[(start, end)] = settings
    return result


def _approved_audio_ranges(
    db: Session,
    video_id: int,
    requested: object,
) -> dict[tuple[float, float], dict[str, Any]]:
    """Resolve completed enhancement ids without trusting a browser-sent URL."""
    if not isinstance(requested, list):
        return {}
    requested_ids: set[int] = set()
    for item in requested:
        if not isinstance(item, dict) or item.get("enhancedResultId") is None:
            continue
        try:
            requested_ids.add(int(item["enhancedResultId"]))
        except (TypeError, ValueError):
            continue
    if not requested_ids:
        return {}
    rows = (
        db.query(AiResult)
        .filter(
            AiResult.id.in_(requested_ids),
            AiResult.video_id == video_id,
            AiResult.result_type == "rough_cut_effect",
            AiResult.status == "completed",
        )
        .all()
    )
    by_id = {int(row.id): row for row in rows}
    approved: dict[tuple[float, float], dict[str, Any]] = {}
    for item in requested:
        if not isinstance(item, dict):
            continue
        try:
            result_id = int(item.get("enhancedResultId"))
            start = round(float(item.get("start", 0)), 3)
            end = round(float(item.get("end", 0)), 3)
        except (TypeError, ValueError):
            continue
        row = by_id.get(result_id)
        if row is None or end <= start:
            continue
        data = row.result_data if isinstance(row.result_data, dict) else {}
        target = data.get("clipTarget") if isinstance(data.get("clipTarget"), dict) else {}
        url = data.get("outputUrl")
        try:
            target_start = round(float(target.get("start", -1)), 3)
            target_end = round(float(target.get("end", -1)), 3)
        except (TypeError, ValueError):
            continue
        if (
            data.get("effectType") != "audio"
            or target_start != start
            or target_end != end
            or not isinstance(url, str)
            or not url.strip()
        ):
            continue
        approved[(start, end)] = {"source": _resolve_media_source(url.strip())}
    return approved


def _match_audio_range(
    table: dict[tuple[float, float], dict[str, Any]],
    start: float,
    end: float,
    *,
    epsilon: float = 0.05,
) -> dict[str, Any] | None:
    """Find the audio settings for a keep range that may have been clamped.

    `_normalize_ranges` clamps every keep range against the probed source
    duration, while these keys were built from the numbers the browser sent --
    and `media.duration` in a browser and ffprobe's duration routinely
    disagree by tens of milliseconds. An exact-key lookup therefore missed on
    the LAST clip of an edit, which silently exported at full volume with no
    fades at all.

    An exact key still wins. Otherwise the range is matched to the entry that
    shares an edge with it (within `epsilon`) and overlaps it most; a span
    that overlaps but lines up with neither edge is a different clip and is
    left unmatched rather than guessed at.

    `colorRanges`, `videoRanges` and `processedRanges` now go through this
    same matcher (the last clip of an edit used to silently lose its grade,
    canvas, or cutout to the identical drift). The processed table is safe to
    match tolerantly because authorization happened when the table was built —
    only completed, owned effect rows ever enter it.
    """
    exact = table.get((round(start, 3), round(end, 3)))
    if exact is not None:
        return exact

    best: dict[str, Any] | None = None
    best_overlap = 0.0
    for (key_start, key_end), value in table.items():
        overlap = min(end, key_end) - max(start, key_start)
        if overlap <= 0:
            continue
        edge_matches = (
            abs(key_start - start) <= epsilon or abs(key_end - end) <= epsilon
        )
        if not edge_matches:
            continue
        if overlap > best_overlap:
            best, best_overlap = value, overlap
    return best


def _muted_source_ranges(raw: object) -> list[tuple[float, float]]:
    """Sanitize muted spans to finite, positive (start, end) source seconds."""
    if not isinstance(raw, list):
        return []
    result: list[tuple[float, float]] = []
    for item in raw[:256]:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item.get("start", 0))
            end = float(item.get("end", 0))
        except (TypeError, ValueError):
            continue
        if math.isfinite(start) and math.isfinite(end) and end > start:
            result.append((max(0.0, start), end))
    return result


def _finite_number(value: Any) -> float:
    """Coerce a client-sent number to a finite float, or 0.0."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _audio_gain_part(audio: dict[str, Any]) -> list[str]:
    """The dB-to-linear gain filter, or nothing at unity."""
    gain_db = max(-60.0, min(60.0, _finite_number(audio.get("volume"))))
    if gain_db == 0.0:
        return []
    return [f"volume={10 ** (gain_db / 20.0):.6f}"]


def _audio_effect_filter_parts(
    audio: dict[str, Any] | None,
    clip_duration: float,
) -> list[str]:
    """Translate volume/fade settings into ffmpeg audio filter parts.

    Gain arrives in dB and becomes a linear multiplier here so the filter
    string never carries user text. Fades clamp to the clip so a stale value
    longer than the clip cannot push `st` negative. The order (fades, then
    gain) must match every place a chain is assembled -- WAV and MP4 segments
    have to sound identical or the two export formats disagree.

    This is the A-roll segment's chain, where one keep range is one clip.
    Timeline layers are not: a cut splits one clip across several chunks, and
    they use `_layer_audio_filter_parts` so the fade stays a property of the
    clip rather than of the fragment.
    """
    if not isinstance(audio, dict) or clip_duration <= 0:
        return []

    parts: list[str] = []
    fade_in = min(clip_duration, max(0.0, _finite_number(audio.get("fadeIn"))))
    fade_out = min(clip_duration, max(0.0, _finite_number(audio.get("fadeOut"))))
    if fade_in > 0:
        parts.append(f"afade=t=in:st=0:d={fade_in:.6f}")
    if fade_out > 0:
        parts.append(
            f"afade=t=out:st={max(0.0, clip_duration - fade_out):.6f}:d={fade_out:.6f}"
        )
    parts.extend(_audio_gain_part(audio))
    return parts


def _layer_audio_filter_parts(
    audio: dict[str, Any] | None,
    *,
    layer_duration: float,
    chunk_offset: float,
    chunk_duration: float,
) -> list[str]:
    """One chunk's share of a timeline layer's volume/fade settings.

    A layer that a cut splits arrives here as several chunks. Applying the
    clip's fades to each of them -- which is what feeding the chunk's own
    length to `_audio_effect_filter_parts` did -- faded the clip in and out
    once per fragment, so a B-roll bed dipped to silence at every cut while
    the editor's preview faded once across the whole clip.

    The fades belong to the LAYER, so they are measured in layer-local
    seconds: `chunk_offset` is where this chunk starts inside the layer and
    `layer_duration` is the layer's whole span. Only the part of each fade
    window that actually falls inside the chunk is emitted:

    * fade entirely outside the chunk -> nothing at all;
    * fade starting at (or before) the chunk's own zero -> plain `afade`,
      which is what an uncut clip has always produced;
    * chunk opening partway up or down a ramp -> a `volume` expression, since
      `afade` cannot start mid-curve and clamping `st` to zero would put back
      the very dip this exists to remove.

    Times are chunk-local because the input was already cut by `-ss`/`-t`,
    which is also why this must run before `adelay`.
    """
    if not isinstance(audio, dict) or chunk_duration <= 0:
        return []
    layer_duration = max(float(layer_duration), float(chunk_duration))
    offset = max(0.0, float(chunk_offset))

    parts: list[str] = []
    fade_in = min(layer_duration, max(0.0, _finite_number(audio.get("fadeIn"))))
    fade_out = min(layer_duration, max(0.0, _finite_number(audio.get("fadeOut"))))

    if fade_in > 0.001 and offset < fade_in - 0.001:
        if offset <= 0.001:
            parts.append(f"afade=t=in:st=0:d={fade_in:.6f}")
        else:
            parts.append(
                f"volume='min(1,(t+{offset:.6f})/{fade_in:.6f})':eval=frame"
            )

    if fade_out > 0.001:
        # Where the fade-out begins, in this chunk's own seconds. Negative
        # means it began before the chunk did.
        fade_out_start = layer_duration - fade_out - offset
        if fade_out_start < chunk_duration - 0.001:
            if fade_out_start >= -0.001:
                parts.append(
                    f"afade=t=out:st={max(0.0, fade_out_start):.6f}:d={fade_out:.6f}"
                )
            else:
                parts.append(
                    f"volume='max(0,min(1,({layer_duration:.6f}-(t+{offset:.6f}))"
                    f"/{fade_out:.6f}))':eval=frame"
                )

    parts.extend(_audio_gain_part(audio))
    return parts


def _segment_audio_filter(
    audio: dict[str, Any] | None,
    muted_ranges: list[tuple[float, float]],
    *,
    segment_start: float,
    segment_end: float,
    rate: float = 1.0,
) -> str | None:
    """Build one A-roll segment's `-af` chain, or None to leave audio alone.

    Every time inside the chain is relative to the segment's own zero because
    the segment was already cut with `-ss`; mutes therefore rebase from source
    seconds here. Mutes run first, then fades, then gain -- they compose
    multiplicatively, so only the consistency of the order matters, and it is
    applied per segment precisely so the later `-c copy` concat stays legal.
    """
    duration = segment_end - segment_start
    if duration <= 0:
        return None
    parts: list[str] = []
    for muted_start, muted_end in muted_ranges:
        low = max(0.0, muted_start - segment_start)
        high = min(duration, muted_end - segment_start)
        if high - low > 0.001:
            parts.append(f"volume=0:enable='between(t,{low:.6f},{high:.6f})'")
    if abs(rate - 1.0) > 0.001:
        # Mutes above run in the SOURCE clock (pre-retime); the atempo sits
        # between them and the fades, which then run in the OUTPUT clock with
        # their times compressed to match the retimed clip.
        from app.jobs.rough_cut_effect import _atempo_chain

        parts.extend(_atempo_chain(rate))
        scaled = dict(audio) if isinstance(audio, dict) else None
        if scaled:
            for key in ("fadeIn", "fadeOut"):
                try:
                    scaled[key] = max(0.0, float(scaled.get(key) or 0.0)) / rate
                except (TypeError, ValueError):
                    scaled[key] = 0.0
        parts.extend(_audio_effect_filter_parts(scaled, duration / rate))
    else:
        parts.extend(_audio_effect_filter_parts(audio, duration))
    return ",".join(parts) if parts else None


def _canvas_background_color(settings: dict[str, Any] | None) -> str:
    """Return the safe solid/fallback color for a clip's Canvas fill."""
    video = settings.get("video") if isinstance(settings, dict) else None
    canvas = video.get("canvas") if isinstance(video, dict) else None
    if canvas is True:
        return "black"
    if not isinstance(canvas, dict) or not bool(canvas.get("enabled")):
        return "black"
    color = str(canvas.get("color") or "#000000").strip()
    return f"0x{color[1:]}" if re.fullmatch(r"#[0-9a-fA-F]{6}", color) else "black"


def _safe_canvas_color(value: Any, fallback: str = "black") -> str:
    color = str(value or "").strip()
    return f"0x{color[1:]}" if re.fullmatch(r"#[0-9a-fA-F]{6}", color) else fallback


def _video_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    video = settings.get("video") if isinstance(settings, dict) else None
    return video if isinstance(video, dict) else {}


def _blend_mode(settings: dict[str, Any] | None) -> str:
    """Translate the inspector's CSS names to a safe FFmpeg blend mode."""
    requested = str(_video_settings(settings).get("blendMode") or "normal")
    return {
        "normal": "normal",
        "multiply": "multiply",
        "screen": "screen",
        "overlay": "overlay",
        "soft-light": "softlight",
        "difference": "difference",
        "color-dodge": "dodge",
    }.get(requested, "normal")


def _number_between(value: Any, low: float, high: float, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(number):
        return fallback
    return max(low, min(high, number))


def _keyframe_track(settings: dict[str, Any] | None, channel: str, duration: float) -> list[dict[str, Any]]:
    keyframes = settings.get("keyframes") if isinstance(settings, dict) else None
    raw = keyframes.get(channel) if isinstance(keyframes, dict) else None
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for item in raw[:200]:
        if not isinstance(item, dict):
            continue
        try:
            at = float(item.get("t", 0))
            value = float(item.get("v", 0))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(at) or not math.isfinite(value):
            continue
        result.append({
            "t": max(0.0, min(max(0.0, duration), at)),
            "v": value,
            "easing": str(item.get("easing") or "linear"),
        })
    result.sort(key=lambda item: item["t"])
    deduped: list[dict[str, Any]] = []
    for item in result:
        if deduped and abs(item["t"] - deduped[-1]["t"]) < 0.0005:
            deduped[-1] = item
        else:
            deduped.append(item)
    return deduped


#: Back-ease constants, from the Penner set the whole motion-design world uses.
#: `c1` is how far the curve pulls past its endpoint; `c3` follows from it.
_EASE_C1 = 1.70158
_EASE_C3 = _EASE_C1 + 1.0
_EASE_OVERSHOOT_C1 = _EASE_C1 * 1.525
_EASE_OVERSHOOT_C3 = _EASE_OVERSHOOT_C1 + 1.0


def _back_out_ratio(ratio: str, c1: float, c3: float) -> str:
    """Overshoot past the target and settle back onto it."""
    return f"1+{c3:.10f}*pow(({ratio})-1,3)+{c1:.10f}*pow(({ratio})-1,2)"


def _eased_ratio(ratio: str, easing: str) -> str:
    """One keyframe segment's easing, as an ffmpeg expression.

    Every curve here is closed-form on purpose. A true `cubic-bezier(x1,y1,x2,y2)`
    has to be *solved* — x and y are both cubics in the parameter, so you need
    Newton-Raphson to recover t from progress — and the only way to iterate in an
    ffmpeg expression is `st()`/`ld()` registers. `_channel_expression` nests
    every segment inside the next in one `if()` chain up to 200 keyframes deep,
    all sharing one register file, so those writes would collide across segments.
    Closed forms give the same craft vocabulary (asymmetric ease, anticipation,
    overshoot) with no registers, no iteration, and expressions small enough to
    read.

    The formulas are duplicated in `_lib/keyframes/clip-keyframes.ts`, which is
    what the editor previews with. `tests/fixtures/easing_curves.json` is the
    contract between the two — change a curve there and both sides must follow.
    """
    if easing == "hold":
        return "0"
    if easing == "ease-in":
        return f"({ratio})*({ratio})"
    if easing == "ease-out":
        return f"1-(1-({ratio}))*(1-({ratio}))"
    if easing == "ease-in-out":
        return f"if(lt(({ratio}),0.5),2*({ratio})*({ratio}),1-pow(-2*({ratio})+2,2)/2)"
    # Cubic in-out. Steeper through the middle than the quadratic above, which
    # is what makes a move read as deliberate rather than mechanical.
    if easing == "smooth":
        return _ease_in_out_cubic(ratio)
    # Decelerations. `glide` eases out gently; `snappy` leaves fast and arrives
    # hard, which is the one most short UI-style moves want.
    if easing == "glide":
        return f"1-pow(1-({ratio}),3)"
    if easing == "snappy":
        return f"1-pow(1-({ratio}),5)"
    # Anticipation: pulls *back* before it moves (dips below 0), the oldest
    # trick in animation for making a move feel intentional.
    if easing == "anticipate":
        return f"{_EASE_C3:.10f}*pow(({ratio}),3)-{_EASE_C1:.10f}*pow(({ratio}),2)"
    if easing == "settle":
        return _back_out_ratio(ratio, _EASE_C1, _EASE_C3)
    if easing == "overshoot":
        return _back_out_ratio(ratio, _EASE_OVERSHOOT_C1, _EASE_OVERSHOOT_C3)
    return ratio


def _channel_expression(
    settings: dict[str, Any] | None,
    channel: str,
    base: float,
    duration: float,
    *,
    low: float,
    high: float,
    time_var: str = "t",
) -> str:
    track = _keyframe_track(settings, channel, duration)
    if not track:
        return f"{max(low, min(high, base)):.6f}"
    values = [{**item, "v": max(low, min(high, float(item["v"])))} for item in track]
    expression = f"{values[-1]['v']:.6f}"
    for index in range(len(values) - 2, -1, -1):
        start = values[index]
        end = values[index + 1]
        span = max(0.0005, end["t"] - start["t"])
        ratio = f"(({time_var})-{start['t']:.6f})/{span:.6f}"
        eased = _eased_ratio(ratio, start["easing"])
        interpolated = f"{start['v']:.6f}+({end['v'] - start['v']:.6f})*({eased})"
        expression = f"if(lt(({time_var}),{end['t']:.6f}),{interpolated},{expression})"
    return f"if(lte(({time_var}),{values[0]['t']:.6f}),{values[0]['v']:.6f},{expression})"


def _animation_presets(settings: dict[str, Any] | None) -> tuple[str, str, str, float, float]:
    animation = settings.get("animation") if isinstance(settings, dict) else None
    if not isinstance(animation, dict):
        return ("none", "none", "none", 0.55, 1.0)
    mode = str(animation.get("mode") or "in")
    legacy = str(animation.get("preset") or "none")
    in_preset = str(animation.get("inPreset") or (legacy if mode == "in" else "none"))
    out_preset = str(animation.get("outPreset") or (legacy if mode == "out" else "none"))
    combo_preset = str(animation.get("comboPreset") or (legacy if mode == "combo" else "none"))
    allowed = {"none", "fade", "zoom", "pop", "slide-left", "slide-right", "slide-up", "spin", "swing", "shake", "pulse", "focus"}
    duration = _number_between(animation.get("duration"), 0.12, 3.0, 0.55)
    intensity = _number_between(animation.get("intensity"), 0.0, 200.0, 100.0) / 100.0
    return (
        in_preset if in_preset in allowed else "none",
        out_preset if out_preset in allowed else "none",
        combo_preset if combo_preset in allowed else "none",
        duration,
        intensity,
    )


def _ease_in_out_cubic(ratio: str) -> str:
    """Cubic ease-in-out, matching `easeInOut` in `_lib/animation/clip-animation.ts`.

    Deliberately cubic and deliberately not the quadratic `ease-in-out` used by
    the keyframe easings above: the two are different curves in the editor too,
    and the render only looks like the preview if each is reproduced as written.
    A looping animation that ramps linearly here while easing in the viewer is
    the difference an editor reads as cheap.
    """
    return f"if(lt(({ratio}),0.5),4*({ratio})*({ratio})*({ratio}),1-pow(-2*({ratio})+2,3)/2)"


def _preset_expressions(preset: str, progress: str, intensity: float) -> dict[str, str]:
    identity = {"x": "0", "y": "0", "scale": "1", "rotation": "0", "opacity": "1"}
    inverse = f"pow(1-({progress}),3)"
    if preset == "fade":
        return {**identity, "opacity": progress}
    if preset in {"zoom", "focus"}:
        amount = 0.32 if preset == "zoom" else 0.06
        return {**identity, "scale": f"1+({inverse})*{amount * intensity:.6f}", "opacity": f"min(1,({progress})*{1.8 if preset == 'zoom' else 2:.3f})"}
    if preset == "pop":
        overshoot = f"if(lt(({progress}),0.72),0.76+(({progress})/0.72)*0.32,1.08-((({progress})-0.72)/0.28)*0.08)"
        return {**identity, "scale": f"1+(({overshoot})-1)*{intensity:.6f}", "opacity": f"min(1,({progress})*2.5)"}
    if preset in {"slide-left", "slide-right", "slide-up"}:
        sign = -42 if preset == "slide-left" else 42 if preset == "slide-right" else 34
        axis = "y" if preset == "slide-up" else "x"
        return {**identity, axis: f"({inverse})*{sign * intensity:.6f}", "opacity": f"min(1,({progress})*1.7)"}
    if preset == "spin":
        return {**identity, "rotation": f"({inverse})*{-18 * intensity:.6f}", "scale": f"1-({inverse})*{0.14 * intensity:.6f}", "opacity": f"min(1,({progress})*1.8)"}
    if preset == "swing":
        return {**identity, "rotation": f"sin(({progress})*PI*3)*({inverse})*{9 * intensity:.6f}", "opacity": f"min(1,({progress})*2)"}
    return identity


def _animation_expressions(settings: dict[str, Any] | None, duration: float) -> dict[str, str]:
    in_preset, out_preset, combo, requested_duration, intensity = _animation_presets(settings)
    active_duration = max(0.12, min(requested_duration, max(0.12, duration / 2)))
    phase = f"max(0,min(1,t/{max(0.01, duration):.6f}))"
    result = {"x": "0", "y": "0", "scale": "1", "rotation": "0", "opacity": "1"}
    if combo == "shake":
        result.update({"x": f"sin(({phase})*PI*8)*{2.5 * intensity:.6f}", "rotation": f"sin(({phase})*PI*6)*{0.8 * intensity:.6f}"})
    elif combo in {"pulse", "pop"}:
        result["scale"] = f"1+sin(({phase})*PI*2)*{0.035 * intensity:.6f}"
    elif combo == "swing":
        result["rotation"] = f"sin(({phase})*PI*2)*{3 * intensity:.6f}"
    elif combo == "zoom":
        result["scale"] = f"1+({_ease_in_out_cubic(phase)})*{0.12 * intensity:.6f}"
    elif combo == "spin":
        result["rotation"] = f"({_ease_in_out_cubic(phase)})*{12 * intensity:.6f}"
    elif combo in {"slide-left", "slide-right"}:
        result["x"] = (
            f"({_ease_in_out_cubic(phase)})*"
            f"{(-8 if combo == 'slide-left' else 8) * intensity:.6f}"
        )
    elif combo == "fade":
        result["opacity"] = f"0.75+sin(({phase})*PI)*0.25"

    if in_preset != "none":
        progress = f"max(0,min(1,t/{active_duration:.6f}))"
        entered = _preset_expressions(in_preset, progress, intensity)
        for key in result:
            identity = "1" if key in {"scale", "opacity"} else "0"
            entered[key] = f"if(lte(t,{active_duration:.6f}),{entered[key]},{identity})"
        sample = entered
        result = {
            "x": f"({result['x']})+({sample['x']})", "y": f"({result['y']})+({sample['y']})",
            "scale": f"({result['scale']})*({sample['scale']})", "rotation": f"({result['rotation']})+({sample['rotation']})",
            "opacity": f"({result['opacity']})*({sample['opacity']})",
        }
    if out_preset != "none":
        remaining = f"{duration:.6f}-t"
        progress = f"max(0,min(1,({remaining})/{active_duration:.6f}))"
        exited = _preset_expressions(out_preset, progress, intensity)
        for key in result:
            identity = "1" if key in {"scale", "opacity"} else "0"
            exited[key] = f"if(lte(({remaining}),{active_duration:.6f}),{exited[key]},{identity})"
        result = {
            "x": f"({result['x']})+({exited['x']})", "y": f"({result['y']})+({exited['y']})",
            "scale": f"({result['scale']})*({exited['scale']})", "rotation": f"({result['rotation']})+({exited['rotation']})",
            "opacity": f"({result['opacity']})*({exited['opacity']})",
        }
    return result


def _needs_clip_compositor(settings: dict[str, Any] | None) -> bool:
    if not isinstance(settings, dict):
        return False
    video = _video_settings(settings)
    if settings.get("mirror") is True or _number_between(settings.get("rotation"), 0, 270, 0) != 0:
        return True
    canvas = video.get("canvas")
    if isinstance(canvas, dict) and bool(canvas.get("enabled")) and canvas.get("mode") in {"gradient", "blur"}:
        return True
    if _blend_mode(settings) != "normal":
        return True
    crop = video.get("crop")
    if isinstance(crop, dict) and bool(crop.get("enabled")) and any(
        abs(_number_between(crop.get(edge), 0, _CROP_MAX_PER_EDGE, 0)) > 0.0001 for edge in _CROP_EDGES
    ):
        return True
    if _dynamic_zoom_settings(settings) is not None:
        return True
    numeric_defaults ={"scale": 100, "scaleY": 100, "x": 0, "y": 0, "rotation": 0, "opacity": 100, "cornerRadius": 0}
    if any(abs(_number_between(video.get(key), -10000, 10000, default) - default) > 0.0001 for key, default in numeric_defaults.items()):
        return True
    keyframes = settings.get("keyframes")
    if isinstance(keyframes, dict) and any(str(key).startswith("video.") and isinstance(value, list) and value for key, value in keyframes.items()):
        return True
    return any(preset != "none" for preset in _animation_presets(settings)[:3])


def _canvas_background_chain(
    settings: dict[str, Any] | None,
    *,
    size: str,
    width: int,
    height: int,
    duration: float,
    frame_rate: float,
    blur_source: str,
) -> tuple[list[str], str]:
    video = _video_settings(settings)
    canvas = video.get("canvas")
    canvas = canvas if isinstance(canvas, dict) and bool(canvas.get("enabled")) else {}
    mode = str(canvas.get("mode") or "color")
    if mode == "blur":
        blur = _number_between(canvas.get("blur"), 0, 80, 28)
        dim = _number_between(canvas.get("dim"), 0, 80, 16) / 100
        return ([f"{blur_source}scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},gblur=sigma={blur:.3f}:steps=2,eq=brightness={-dim:.4f}[canvasbg]"], "[canvasbg]")
    color = _safe_canvas_color(canvas.get("color"), "black")
    if mode == "gradient":
        color_end = _safe_canvas_color(canvas.get("colorEnd"), "0x151826")
        angle = math.radians(_number_between(canvas.get("angle"), 0, 360, 135) - 90)
        radius = math.hypot(width, height) / 2
        center_x, center_y = width / 2, height / 2
        dx, dy = math.cos(angle) * radius, math.sin(angle) * radius
        x0 = max(0, min(width, round(center_x - dx)))
        y0 = max(0, min(height, round(center_y - dy)))
        x1 = max(0, min(width, round(center_x + dx)))
        y1 = max(0, min(height, round(center_y + dy)))
        source = (
            f"gradients=s={size}:r={max(1, frame_rate):.6f}:d={duration:.6f}:speed=0:"
            f"c0={color}:c1={color_end}:x0={x0}:y0={y0}:x1={x1}:y1={y1}[canvasbg]"
        )
        return ([source], "[canvasbg]")
    return ([f"color={color}:s={size}:r={max(1, frame_rate):.6f}:d={duration:.6f}[canvasbg]"], "[canvasbg]")


def _motion_blur_filter_parts(settings: dict[str, Any] | None) -> list[str]:
    """Build a bounded temporal blur. `tmix` blends actual neighbouring
    frames, unlike a spatial CSS blur, so moving edges trail while a static
    shot remains sharp."""
    video = settings.get("video") if isinstance(settings, dict) else None
    motion = video.get("motionBlur") if isinstance(video, dict) else None
    if motion is True:
        motion = {"enabled": True, "amount": 20, "shutterAngle": 180}
    if not isinstance(motion, dict) or not bool(motion.get("enabled")):
        return []
    try:
        amount = max(0.0, min(100.0, float(motion.get("amount", 20))))
        shutter = max(45.0, min(360.0, float(motion.get("shutterAngle", 180))))
    except (TypeError, ValueError):
        return []
    if amount <= 0.01:
        return []
    frames = max(2, min(8, round(2 + (amount / 100.0) * 4 * (shutter / 180.0))))
    return [f"tmix=frames={frames}:weights='{' '.join(['1'] * frames)}'"]


# Crop limits, mirrored from `_lib/viewer/clip-crop.ts`. The far edge of each
# axis yields so the two can never cross, which is the one clamp that survives
# being written as an ffmpeg expression over keyframed values.
_CROP_MAX_PER_EDGE = 90.0
_CROP_MIN_REMAINING = 2.0
_CROP_MAX_SOFTNESS = 50.0
_CROP_EDGES = ("left", "right", "top", "bottom")


def _crop_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    crop = _video_settings(settings).get("crop")
    return crop if isinstance(crop, dict) else {}


def _crop_edge_expressions(
    settings: dict[str, Any] | None,
    duration: float,
    *,
    time_var: str = "T",
) -> dict[str, str]:
    """Each crop edge as a percentage expression, keyframes included.

    `geq` reads pixel time as `T`; `overlay` reads frame time as `t`. A crop
    animated on the wrong clock renders the whole clip at time zero.
    """
    crop = _crop_settings(settings)
    channels = {
        "left": "video.cropLeft",
        "right": "video.cropRight",
        "top": "video.cropTop",
        "bottom": "video.cropBottom",
        "softness": "video.cropSoftness",
    }
    result: dict[str, str] = {}
    for field, channel in channels.items():
        high = _CROP_MAX_SOFTNESS if field == "softness" else _CROP_MAX_PER_EDGE
        result[field] = _channel_expression(
            settings,
            channel,
            _number_between(crop.get(field), 0, high, 0),
            duration,
            low=0,
            high=high,
            time_var=time_var,
        )
    return result


def _crop_is_active(settings: dict[str, Any] | None, duration: float) -> bool:
    """Mirrors `resolveClipCrop` plus the sampling rule above it.

    An animated crop draws unless the section was explicitly switched off —
    `sampleClipAttributes` turns one on in the editor for the same reason, since
    a track that animates an edge nobody draws is a track that does nothing.
    """
    crop = _crop_settings(settings)
    enabled = crop.get("enabled")
    if any(
        bool(_keyframe_track(settings, f"video.crop{edge.capitalize()}", duration))
        for edge in _CROP_EDGES
    ):
        return enabled is not False
    if not enabled:
        return False
    return any(_number_between(crop.get(edge), 0, _CROP_MAX_PER_EDGE, 0) > 0.001 for edge in _CROP_EDGES)


def _crop_alpha_expression(settings: dict[str, Any] | None, duration: float) -> str | None:
    """Alpha multiplier that cuts the crop into the clip, feathered by softness.

    Evaluated inside `geq`, before the rotate/scale below, so the crop is cut in
    the clip's own image space and travels with the footage — the same order
    `clipVisualStyle` gets from applying `clip-path` ahead of `transform`.
    """
    if not _crop_is_active(settings, duration):
        return None
    edges = _crop_edge_expressions(settings, duration)
    x0 = f"W*(({edges['left']})/100)"
    y0 = f"H*(({edges['top']})/100)"
    # The far edge yields: never let it cross the near one (see `axis` in
    # clip-crop.ts), so an over-cropped axis leaves a strip rather than nothing.
    x1 = f"max(({x0})+W*{_CROP_MIN_REMAINING / 100:.4f},W*(1-(({edges['right']})/100)))"
    y1 = f"max(({y0})+H*{_CROP_MIN_REMAINING / 100:.4f},H*(1-(({edges['bottom']})/100)))"
    # Softness is a share of the shorter edge, like cornerRadius. Floored at
    # half a pixel so a hard crop is still a one-expression ramp.
    feather = f"max(0.5,min(W,H)*(({edges['softness']})/100))"

    def ramp(edge: str, distance: str) -> str:
        """Fade in over the softness — but only on an edge that is cropped.

        An uncropped edge stays hard: feathering it would fade the picture away
        from the frame border, which is a vignette rather than a crop. Written
        as a guard rather than dropped at build time so a keyframed edge starts
        feathering the moment it starts cutting. Mirrors `cropMaskLayers`.
        """
        return f"if(lte(({edges[edge]}),0.0001),1,clip(({distance})/({feather}),0,1))"

    return (
        f"min(min({ramp('left', f'X-({x0})')},{ramp('right', f'({x1})-X')}),"
        f"min({ramp('top', f'Y-({y0})')},{ramp('bottom', f'({y1})-Y')}))"
    )


def _crop_position_compensation(
    settings: dict[str, Any] | None,
    duration: float,
    *,
    scale_x: str,
    scale_y: str,
    angle: str,
    time_var: str = "t",
) -> tuple[str, str]:
    """Offset that moves the transform's origin onto the cropped area's centre.

    ffmpeg scales and rotates the full-size frame about its own centre, which is
    "Retain Image Position" behaviour. Off (the default), Resolve scales about
    the cropped bounding box instead, which is what the preview gets from
    `transform-origin`. The difference is the crop centre `o` minus where a
    frame-centred transform would have carried it, `S·R·o` — CSS applies the
    rotation to the point first, then the scale, so this does too.
    """
    crop = _crop_settings(settings)
    if not crop or bool(crop.get("retainPosition")) or not _crop_is_active(settings, duration):
        return ("0", "0")
    edges = _crop_edge_expressions(settings, duration, time_var=time_var)
    # Crop centre as a signed fraction of each axis, measured from the middle.
    offset_x = f"W*(((({edges['left']})+(100-({edges['right']})))/200)-0.5)"
    offset_y = f"H*(((({edges['top']})+(100-({edges['bottom']})))/200)-0.5)"
    rotated_x = f"cos({angle})*({offset_x})-sin({angle})*({offset_y})"
    rotated_y = f"sin({angle})*({offset_x})+cos({angle})*({offset_y})"
    return (
        f"({offset_x})-({scale_x})*({rotated_x})",
        f"({offset_y})-({scale_y})*({rotated_y})",
    )


# Dynamic Zoom limits and defaults, mirrored from `_lib/viewer/dynamic-zoom.ts`.
_DYNAMIC_ZOOM_EASES = ("linear", "ease-in", "ease-out", "ease-in-out")
_DYNAMIC_ZOOM_START = {"scale": 100.0, "x": 0.0, "y": 0.0}
_DYNAMIC_ZOOM_END = {"scale": 120.0, "x": 0.0, "y": 0.0}
_DYNAMIC_ZOOM_MIN_SCALE = 10.0
_DYNAMIC_ZOOM_MAX_SCALE = 400.0
_DYNAMIC_ZOOM_MAX_OFFSET = 100.0


def _dynamic_zoom_box(raw: Any, fallback: dict[str, float]) -> dict[str, float]:
    box = raw if isinstance(raw, dict) else {}
    return {
        "scale": _number_between(box.get("scale"), _DYNAMIC_ZOOM_MIN_SCALE, _DYNAMIC_ZOOM_MAX_SCALE, fallback["scale"]),
        "x": _number_between(box.get("x"), -_DYNAMIC_ZOOM_MAX_OFFSET, _DYNAMIC_ZOOM_MAX_OFFSET, fallback["x"]),
        "y": _number_between(box.get("y"), -_DYNAMIC_ZOOM_MAX_OFFSET, _DYNAMIC_ZOOM_MAX_OFFSET, fallback["y"]),
    }


def _dynamic_zoom_settings(settings: dict[str, Any] | None) -> dict[str, Any] | None:
    """The clip's Dynamic Zoom, or None when it would not move the picture."""
    zoom = _video_settings(settings).get("dynamicZoom")
    if not isinstance(zoom, dict) or not bool(zoom.get("enabled")):
        return None
    start = _dynamic_zoom_box(zoom.get("start"), _DYNAMIC_ZOOM_START)
    end = _dynamic_zoom_box(zoom.get("end"), _DYNAMIC_ZOOM_END)
    if all(abs(start[key] - end[key]) <= 0.001 for key in ("scale", "x", "y")):
        return None
    ease = str(zoom.get("ease") or "linear")
    return {"start": start, "end": end, "ease": ease if ease in _DYNAMIC_ZOOM_EASES else "linear"}


def _dynamic_zoom_expressions(settings: dict[str, Any] | None, duration: float) -> dict[str, str]:
    """Dynamic Zoom as a scale multiplier and a pair of percent offsets.

    The move is spread across the clip's whole duration rather than pinned to
    seconds, which is what makes it survive a trim — the same `progress` the
    editor samples in `sampleDynamicZoom`. Composed like an animation preset:
    multiplied into scale, added to position.
    """
    zoom = _dynamic_zoom_settings(settings)
    if not zoom:
        return {"scale": "1", "x": "0", "y": "0"}
    span = max(0.0001, duration)
    progress = f"max(0,min(1,t/{span:.6f}))"
    ratio = _eased_ratio(progress, zoom["ease"])
    start, end = zoom["start"], zoom["end"]

    def between(key: str, divisor: float = 1.0) -> str:
        delta = (end[key] - start[key]) / divisor
        return f"({start[key] / divisor:.6f}+({delta:.6f})*({ratio}))"

    return {"scale": between("scale", 100.0), "x": between("x"), "y": between("y")}


def _clip_compositor_filter_complex(
    *,
    vf: str,
    scale_w: int,
    scale_h: int,
    duration: float,
    frame_rate: float,
    settings: dict[str, Any] | None,
    processed: bool,
    matte_input: int | None,
) -> str:
    """Compose a source clip with its Canvas, transform, animation and alpha.

    Every expression is constructed from clamped numbers and whitelisted
    preset ids. No browser-provided filter text enters this graph.
    """
    size = f"{scale_w}x{scale_h}"
    video = _video_settings(settings)
    canvas = video.get("canvas")
    blur_canvas = isinstance(canvas, dict) and bool(canvas.get("enabled")) and canvas.get("mode") == "blur"
    graph: list[str] = []

    if blur_canvas and not processed:
        graph.append("[0:v]split=2[foreground_source][canvas_source]")
        foreground_source = "[foreground_source]"
        blur_source = "[canvas_source]"
    else:
        foreground_source = "[0:v]"
        # A processed cutout is input 0; input 1 remains the original source.
        blur_source = "[1:v]" if processed else "[0:v]"

    graph.append(f"{foreground_source}{vf}[foreground]")
    cutout = "[foreground]"
    if matte_input is not None:
        graph.extend([
            "[foreground]split[foreground_rgb][foreground_alpha_source]",
            "[foreground_alpha_source]alphaextract[foreground_alpha]",
            f"[{matte_input}:v]format=gray[mask_alpha]",
            "[foreground_alpha][mask_alpha]blend=all_mode=multiply[combined_alpha]",
            "[foreground_rgb][combined_alpha]alphamerge[cutout]",
        ])
        cutout = "[cutout]"

    if isinstance(settings, dict) and settings.get("mirror") is True:
        graph.append(f"{cutout}hflip[mirrored_cutout]")
        cutout = "[mirrored_cutout]"

    animation = _animation_expressions(settings, duration)
    scale_x = _channel_expression(settings, "video.scale", _number_between(video.get("scale"), 1, 400, 100), duration, low=1, high=400)
    scale_y_base = _number_between(video.get("scaleY"), 1, 400, _number_between(video.get("scale"), 1, 400, 100))
    scale_y = _channel_expression(settings, "video.scaleY", scale_y_base, duration, low=1, high=400)
    x = _channel_expression(settings, "video.x", _number_between(video.get("x"), -1000, 1000, 0), duration, low=-1000, high=1000)
    y = _channel_expression(settings, "video.y", _number_between(video.get("y"), -1000, 1000, 0), duration, low=-1000, high=1000)
    root_rotation = _number_between(settings.get("rotation") if isinstance(settings, dict) else 0, 0, 270, 0)
    rotation = _channel_expression(settings, "video.rotation", _number_between(video.get("rotation"), -3600, 3600, 0), duration, low=-3600, high=3600)
    opacity = _channel_expression(settings, "video.opacity", _number_between(video.get("opacity"), 0, 100, 100), duration, low=0, high=100, time_var="T")
    corner = _channel_expression(settings, "video.cornerRadius", _number_between(video.get("cornerRadius"), 0, 50, 0), duration, low=0, high=50, time_var="T")

    animation_opacity_at_pixel_time = re.sub(r"\bt\b", "T", animation["opacity"])
    opacity_expression = f"(({opacity})/100)*({animation_opacity_at_pixel_time})"
    radius = f"min(W,H)*(({corner})/100)"
    corner_alpha = (
        f"if(between(X,({radius}),W-({radius})),1,"
        f"if(between(Y,({radius}),H-({radius})),1,"
        f"lte(pow(X-if(lt(X,({radius})),({radius}),W-({radius})),2)+"
        f"pow(Y-if(lt(Y,({radius})),({radius}),H-({radius})),2),pow(({radius}),2))))"
    )
    crop_alpha = _crop_alpha_expression(settings, duration)
    presets = _animation_presets(settings)
    alpha_active = (
        abs(_number_between(video.get("opacity"), 0, 100, 100) - 100) > 0.0001
        or abs(_number_between(video.get("cornerRadius"), 0, 50, 0)) > 0.0001
        or bool(_keyframe_track(settings, "video.opacity", duration))
        or bool(_keyframe_track(settings, "video.cornerRadius", duration))
        or crop_alpha is not None
        or presets[0] != "none"
        or presets[1] != "none"
        or presets[2] == "fade"
    )
    alpha_clip = cutout
    if alpha_active:
        graph.append(
            f"{cutout}geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
            f"a='alpha(X,Y)*({opacity_expression})*({corner_alpha})*({crop_alpha or 1})'[alpha_clip]"
        )
        alpha_clip = "[alpha_clip]"

    rotation_active = (
        abs(_number_between(video.get("rotation"), -3600, 3600, 0) + root_rotation) > 0.0001
        or bool(_keyframe_track(settings, "video.rotation", duration))
        or presets[0] in {"spin", "swing"}
        or presets[1] in {"spin", "swing"}
        or presets[2] in {"spin", "swing", "shake"}
    )
    transformed = alpha_clip
    base_width, base_height = scale_w, scale_h
    if rotation_active:
        diagonal = max(2, int(math.ceil(math.hypot(scale_w, scale_h) / 2) * 2))
        angle = f"(({rotation})+({root_rotation:.6f})+({animation['rotation']}))*PI/180"
        graph.append(f"{alpha_clip}rotate=angle='{angle}':ow={diagonal}:oh={diagonal}:c=none[rotated]")
        transformed = "[rotated]"
        base_width = base_height = diagonal

    zoom = _dynamic_zoom_expressions(settings, duration)
    scale_x_expression = f"min(6,max(0.01,(({scale_x})/100)*({animation['scale']})*({zoom['scale']})))"
    scale_y_expression = f"min(6,max(0.01,(({scale_y})/100)*({animation['scale']})*({zoom['scale']})))"
    graph.append(
        f"{transformed}scale=w='trunc(max(2,{base_width}*({scale_x_expression}))/2)*2':"
        f"h='trunc(max(2,{base_height}*({scale_y_expression}))/2)*2':eval=frame[scaled_clip]"
    )

    background_graph, background = _canvas_background_chain(
        settings,
        size=size,
        width=scale_w,
        height=scale_h,
        duration=duration,
        frame_rate=frame_rate,
        blur_source=blur_source,
    )
    graph.extend(background_graph)
    crop_shift_x, crop_shift_y = _crop_position_compensation(
        settings,
        duration,
        scale_x=scale_x_expression,
        scale_y=scale_y_expression,
        angle=f"(({rotation})+({root_rotation:.6f})+({animation['rotation']}))*PI/180",
    )
    overlay_x = f"(W-w)/2+((({x})+({animation['x']})+({zoom['x']}))/100)*W+({crop_shift_x})"
    overlay_y = f"(H-h)/2+((({y})+({animation['y']})+({zoom['y']}))/100)*H+({crop_shift_y})"
    blend_mode = _blend_mode(settings)
    if blend_mode == "normal":
        graph.append(f"{background}[scaled_clip]overlay=x='{overlay_x}':y='{overlay_y}':shortest=1[composited]")
    else:
        # `blend` requires equally-sized inputs and does not honor the top
        # input's alpha by itself. First place the transformed clip on a
        # transparent full-frame surface, calculate the blend, then merge the
        # result over the untouched Canvas using the clip alpha. This matches
        # CSS mix-blend-mode outside the clip bounds instead of darkening the
        # whole frame with transparent black.
        graph.extend([
            f"color=black@0:s={size}:r={max(1, frame_rate):.6f}:d={duration:.6f},format=rgba[blend_clear]",
            f"[blend_clear][scaled_clip]overlay=x='{overlay_x}':y='{overlay_y}':shortest=1:format=auto[blend_foreground]",
            "[blend_foreground]split[blend_foreground_rgb_source][blend_alpha_source]",
            "[blend_alpha_source]alphaextract[blend_alpha]",
            "[blend_foreground_rgb_source]format=gbrp[blend_foreground_rgb]",
            f"{background}format=gbrp,split[blend_base][blend_background]",
            f"[blend_foreground_rgb][blend_background]blend=all_mode={blend_mode}[blend_result]",
            "[blend_base][blend_result][blend_alpha]maskedmerge[composited]",
        ])

    motion = _motion_blur_filter_parts(settings)
    if motion:
        graph.append(f"[composited]{','.join(motion)}[v]")
    else:
        graph.append("[composited]null[v]")
    return ";".join(graph)


def _video_segment_command(
    *,
    video_source: str,
    audio_source: str,
    source_start: float,
    duration: float,
    vf: str,
    scale_w: int,
    scale_h: int,
    crf: int,
    output: Path,
    processed: bool,
    matte_path: Path | None = None,
    background_color: str = "black",
    clip_settings: dict[str, Any] | None = None,
    frame_rate: float = 30.0,
    audio_filter: str | None = None,
    audio_processed: bool = False,
) -> list[str]:
    """Build one MP4 segment while preserving a processed source's alpha."""
    command = ["ffmpeg", "-y"]
    if processed or audio_processed:
        if not processed and source_start > 0:
            command += ["-ss", str(source_start)]
        command += ["-i", video_source]
        if not audio_processed and source_start > 0:
            command += ["-ss", str(source_start)]
        command += ["-i", audio_source]
        audio_input = 1
    else:
        command += ["-ss", str(source_start), "-i", video_source]
        audio_input = 0

    matte_input: int | None = None
    if matte_path is not None:
        matte_input = 2 if (processed or audio_processed) else 1
        command += ["-i", str(matte_path)]

    command += ["-t", str(duration)]
    if _needs_clip_compositor(clip_settings):
        command += [
            "-filter_complex",
            _clip_compositor_filter_complex(
                vf=vf,
                scale_w=scale_w,
                scale_h=scale_h,
                duration=duration,
                frame_rate=frame_rate,
                settings=clip_settings,
                processed=processed,
                matte_input=matte_input,
            ),
            "-map",
            "[v]",
        ]
    elif matte_input is not None:
        command += [
            "-filter_complex",
            _masked_filter_complex(
                vf,
                scale_w,
                scale_h,
                matte_input=matte_input,
                background_color=background_color,
            ),
            "-map",
            "[v]",
        ]
    elif processed:
        color_size = f"{scale_w}x{scale_h}"
        command += [
            "-filter_complex",
            f"[0:v]{vf}[cutout];color={background_color}:s={color_size}[bg];"
            "[bg][cutout]overlay=shortest=1[v]",
            "-map",
            "[v]",
        ]
    else:
        command += ["-vf", vf, "-map", "0:v"]

    command += [
        "-map",
        f"{audio_input}:a?",
    ]
    if audio_filter:
        # `-af` may ride beside `-filter_complex` because the complex graph
        # only ever consumes video and matte streams; audio always re-encodes
        # to aac below, so filtering it costs no extra transcode.
        command += ["-af", audio_filter]
    command += [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(output),
    ]
    return command


def _timeline_time_expression(expression: str, output_start: float, clip_offset: float) -> str:
    """Translate an overlay expression's global `t` back to clip-local time."""
    local_t = f"(t-{output_start:.6f}+{clip_offset:.6f})"
    return re.sub(r"\bt\b", local_t, expression)


def _timeline_layer_graph(
    chunk: dict[str, Any],
    *,
    input_index: int,
    layer_index: int,
    base_label: str,
    scale_w: int,
    scale_h: int,
    frame_rate: float,
) -> tuple[list[str], str]:
    """Build one safe RGBA picture-layer chain plus its ordered overlay."""
    settings = chunk.get("settings") if isinstance(chunk.get("settings"), dict) else {}
    video = _video_settings(settings)
    clip_duration = max(0.04, float(chunk["end"]) - float(chunk["start"]))
    chunk_duration = max(0.04, float(chunk["outputEnd"]) - float(chunk["outputStart"]))
    clip_offset = max(0.0, float(chunk["clipOffset"]))
    output_start = max(0.0, float(chunk["outputStart"]))
    keyframes = settings.get("keyframes") if isinstance(settings.get("keyframes"), dict) else {}
    adjust = dict(settings.get("adjust")) if isinstance(settings.get("adjust"), dict) else {}
    adjust["keyframes"] = keyframes

    source_label = f"layer{layer_index}_source"
    graph: list[str] = []
    source_filters = [
        f"setpts=PTS-STARTPTS+{clip_offset:.6f}/TB",
        f"scale={scale_w}:{scale_h}:force_original_aspect_ratio=decrease",
        f"pad={scale_w}:{scale_h}:(ow-iw)/2:(oh-ih)/2:color=black@0",
        *build_keyframed_adjust_filter_chain(adjust, clip_duration),
        "format=rgba",
    ]
    graph.append(f"[{input_index}:v]{','.join(source_filters)}[{source_label}]")
    current = f"[{source_label}]"

    if settings.get("mirror") is True:
        mirrored = f"layer{layer_index}_mirrored"
        graph.append(f"{current}hflip[{mirrored}]")
        current = f"[{mirrored}]"

    animation = _animation_expressions(settings, clip_duration)
    scale_x = _channel_expression(
        settings,
        "video.scale",
        _number_between(video.get("scale"), 1, 400, 100),
        clip_duration,
        low=1,
        high=400,
    )
    scale_y = _channel_expression(
        settings,
        "video.scaleY",
        _number_between(video.get("scaleY"), 1, 400, _number_between(video.get("scale"), 1, 400, 100)),
        clip_duration,
        low=1,
        high=400,
    )
    root_rotation = _number_between(settings.get("rotation"), 0, 270, 0)
    rotation = _channel_expression(
        settings,
        "video.rotation",
        _number_between(video.get("rotation"), -3600, 3600, 0),
        clip_duration,
        low=-3600,
        high=3600,
    )
    opacity = _channel_expression(
        settings,
        "video.opacity",
        _number_between(video.get("opacity"), 0, 100, 100),
        clip_duration,
        low=0,
        high=100,
        time_var="T",
    )
    corner = _channel_expression(
        settings,
        "video.cornerRadius",
        _number_between(video.get("cornerRadius"), 0, 50, 0),
        clip_duration,
        low=0,
        high=50,
        time_var="T",
    )
    animation_opacity = re.sub(r"\bt\b", "T", animation["opacity"])
    radius = f"min(W,H)*(({corner})/100)"
    corner_alpha = (
        f"if(between(X,({radius}),W-({radius})),1,"
        f"if(between(Y,({radius}),H-({radius})),1,"
        f"lte(pow(X-if(lt(X,({radius})),({radius}),W-({radius})),2)+"
        f"pow(Y-if(lt(Y,({radius})),({radius}),H-({radius})),2),pow(({radius}),2))))"
    )
    crop_alpha = _crop_alpha_expression(settings, clip_duration)
    alpha_label = f"layer{layer_index}_alpha"
    graph.append(
        f"{current}geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
        f"a='alpha(X,Y)*(({opacity})/100)*({animation_opacity})*({corner_alpha})*({crop_alpha or 1})'[{alpha_label}]"
    )
    current = f"[{alpha_label}]"

    presets = _animation_presets(settings)
    rotation_active = (
        abs(_number_between(video.get("rotation"), -3600, 3600, 0) + root_rotation) > 0.0001
        or bool(_keyframe_track(settings, "video.rotation", clip_duration))
        or presets[0] in {"spin", "swing"}
        or presets[1] in {"spin", "swing"}
        or presets[2] in {"spin", "swing", "shake"}
    )
    base_width, base_height = scale_w, scale_h
    if rotation_active:
        diagonal = max(2, int(math.ceil(math.hypot(scale_w, scale_h) / 2) * 2))
        rotated = f"layer{layer_index}_rotated"
        graph.append(
            f"{current}rotate=angle='(({rotation})+({root_rotation:.6f})+({animation['rotation']}))*PI/180':"
            f"ow={diagonal}:oh={diagonal}:c=none[{rotated}]"
        )
        current = f"[{rotated}]"
        base_width = base_height = diagonal

    zoom = _dynamic_zoom_expressions(settings, clip_duration)
    scale_x_expression = f"min(6,max(0.01,(({scale_x})/100)*({animation['scale']})*({zoom['scale']})))"
    scale_y_expression = f"min(6,max(0.01,(({scale_y})/100)*({animation['scale']})*({zoom['scale']})))"
    scaled = f"layer{layer_index}_scaled"
    graph.append(
        f"{current}scale=w='trunc(max(2,{base_width}*({scale_x_expression}))/2)*2':"
        f"h='trunc(max(2,{base_height}*({scale_y_expression}))/2)*2':eval=frame[{scaled}]"
    )
    current = f"[{scaled}]"
    motion = _motion_blur_filter_parts(settings)
    if motion:
        blurred = f"layer{layer_index}_blurred"
        graph.append(f"{current}{','.join(motion)}[{blurred}]")
        current = f"[{blurred}]"

    timed = f"layer{layer_index}_timed"
    graph.append(f"{current}setpts=PTS-STARTPTS+{output_start:.6f}/TB[{timed}]")
    x = _channel_expression(
        settings,
        "video.x",
        _number_between(video.get("x"), -1000, 1000, 0),
        clip_duration,
        low=-1000,
        high=1000,
    )
    y = _channel_expression(
        settings,
        "video.y",
        _number_between(video.get("y"), -1000, 1000, 0),
        clip_duration,
        low=-1000,
        high=1000,
    )
    local_x = _timeline_time_expression(f"({x})+({animation['x']})+({zoom['x']})", output_start, clip_offset)
    local_y = _timeline_time_expression(f"({y})+({animation['y']})+({zoom['y']})", output_start, clip_offset)
    crop_shift_x, crop_shift_y = _crop_position_compensation(
        settings,
        clip_duration,
        scale_x=scale_x_expression,
        scale_y=scale_y_expression,
        angle=f"(({rotation})+({root_rotation:.6f})+({animation['rotation']}))*PI/180",
    )
    # These land in an overlay expression, whose `t` is timeline time: the same
    # rebase every other animated term on this layer goes through.
    local_crop_shift_x = _timeline_time_expression(crop_shift_x, output_start, clip_offset)
    local_crop_shift_y = _timeline_time_expression(crop_shift_y, output_start, clip_offset)
    output_label = f"timeline_composite_{layer_index}"
    graph.append(
        f"{base_label}[{timed}]overlay="
        f"x='(W-w)/2+(({local_x})/100)*W+({local_crop_shift_x})':"
        f"y='(H-h)/2+(({local_y})/100)*H+({local_crop_shift_y})':"
        f"enable='between(t,{output_start:.6f},{output_start + chunk_duration:.6f})':"
        f"eof_action=pass:repeatlast=0:format=auto[{output_label}]"
    )
    return graph, f"[{output_label}]"


# Audio format every branch is normalised to before it is mixed.
_MIX_AFORMAT = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"


def _is_audio_layer(chunk: dict[str, Any]) -> bool:
    """True for a clip on an audio lane -- music/SFX with no picture at all."""
    return str(chunk.get("kind") or "") == "audio"


def _audible_layer_chunks(
    chunks: list[dict[str, Any]],
) -> list[tuple[int, dict[str, Any]]]:
    """The chunks that contribute sound, paired with their ffmpeg input index.

    The index is the chunk's position in the caller's list plus one for the
    base, so the caller must add ONE input per chunk in the same order --
    including the silent ones -- or the `[N:a]` labels point at the wrong
    stream.
    """
    return [
        (index, chunk)
        for index, chunk in enumerate(chunks)
        if bool(chunk.get("audioEnabled")) and bool(chunk.get("hasAudio"))
    ]


def _timeline_audio_graph(
    chunks: list[dict[str, Any]],
    *,
    base_has_audio: bool,
    duck: bool = False,
) -> tuple[list[str], str | None]:
    """Mix the sound of every audible layer over the base.

    The compositor used to copy the base's audio and throw the layers' away,
    which is right for silent B-roll and wrong for anything placed because of
    what is said in it. Layers arrive already cut to length by `-ss`/`-t`, so
    each one only has to be delayed to where it sits on the output timeline.

    Clips on an audio lane (`kind == "audio"`) go through here exactly like an
    audible picture layer; they differ only in having no frames, which is the
    video graph's problem, not this one's.

    `normalize=0` keeps `amix` from quietly ducking everything by the number of
    inputs -- the base is the programme, not one voice among several.
    """
    audible = _audible_layer_chunks(chunks)
    if not audible:
        return [], None

    graph: list[str] = []
    labels: list[str] = []
    if base_has_audio:
        graph.append(f"[0:a]{_MIX_AFORMAT}[amix_base]")
        labels.append("[amix_base]")
    for position, (index, chunk) in enumerate(audible):
        delay_ms = max(0, int(round(float(chunk["outputStart"]) * 1000)))
        label = f"amix_layer{position}"
        settings = chunk.get("settings") if isinstance(chunk.get("settings"), dict) else {}
        chunk_duration = float(chunk["outputEnd"]) - float(chunk["outputStart"])
        # Gain/fades run before adelay so their clock is the clip's own zero;
        # after the delay `st=0` would fade the inserted silence, not the clip.
        # The fades anchor against the LAYER's span, not this chunk's: a cut
        # splits one clip into several chunks and the clip fades once.
        effects = _layer_audio_filter_parts(
            settings.get("audio"),
            layer_duration=float(chunk.get("layerDuration") or chunk_duration),
            chunk_offset=float(chunk.get("layerOffset") or chunk.get("clipOffset") or 0.0),
            chunk_duration=chunk_duration,
        )
        chain = ",".join([_MIX_AFORMAT, *effects, f"adelay=delays={delay_ms}:all=1"])
        graph.append(f"[{index + 1}:a]{chain}[{label}]")
        labels.append(f"[{label}]")

    if len(labels) == 1:
        # Nothing to mix against: rename rather than run a one-input amix.
        graph.append(f"{labels[0]}anull[a]")
        return graph, "[a]"

    if duck and base_has_audio:
        # Duck the layers under the programme: the base (dialogue) keys a
        # sidechain compressor over the mixed layers, so music drops when
        # someone speaks and swells back in the gaps — the exporter had no
        # ducking at all before this (plan §5.3, no `sidechaincompress` in
        # the repo).
        layer_labels = labels[1:]
        if len(layer_labels) == 1:
            graph.append(f"{layer_labels[0]}anull[duck_layers]")
        else:
            graph.append(
                f"{''.join(layer_labels)}amix=inputs={len(layer_labels)}"
                ":normalize=0:dropout_transition=0[duck_layers]"
            )
        graph.append("[amix_base]asplit=2[duck_voice][duck_key]")
        graph.append(
            "[duck_layers][duck_key]sidechaincompress="
            "threshold=0.03:ratio=8:attack=20:release=400:makeup=1[duck_ducked]"
        )
        graph.append(
            "[duck_voice][duck_ducked]amix=inputs=2:normalize=0:dropout_transition=0[a]"
        )
        return graph, "[a]"

    graph.append(
        f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:dropout_transition=0[a]"
    )
    return graph, "[a]"


def _timeline_layers_command(
    *,
    base_video: Path,
    chunks: list[dict[str, Any]],
    scale_w: int,
    scale_h: int,
    frame_rate: float,
    crf: int,
    output: Path,
    base_has_audio: bool = True,
    duck: bool = False,
) -> list[str]:
    """Compose all remapped chunks in Resolve-style bottom-to-top order.

    Chunks on an audio lane have no picture and so never reach the video
    graph; they are still opened as inputs, in list order, because that order
    is what `_timeline_audio_graph` numbers its `[N:a]` labels from. When the
    pass carries nothing BUT audio lanes there is no compositing left to do,
    so the base's video is stream-copied instead of re-encoded for nothing.
    """
    command = ["ffmpeg", "-y", "-i", str(base_video)]
    for chunk in chunks:
        span = f"{max(0.04, float(chunk['outputEnd']) - float(chunk['outputStart'])):.6f}"
        if bool(chunk.get("isStill")):
            # A still has no timeline to seek and only one frame to decode.
            # `-loop 1` turns it into a stream and `-t` gives it exactly the
            # length the clip is on screen; without the pair the input is either
            # a single frame that ends immediately or an endless one. The
            # framerate is pinned to the timeline's so the looped frames line up
            # with the base instead of arriving at ffmpeg's 25fps default.
            command += [
                "-loop",
                "1",
                "-framerate",
                f"{max(1.0, float(frame_rate)):.6f}",
                "-t",
                span,
                "-i",
                str(chunk["source"]),
            ]
        else:
            command += [
                "-ss",
                f"{max(0.0, float(chunk['sourceSeek'])):.6f}",
                "-t",
                span,
                "-i",
                str(chunk["source"]),
            ]

    has_picture_layers = any(not _is_audio_layer(chunk) for chunk in chunks)
    graph: list[str] = []
    if has_picture_layers:
        graph.append("[0:v]setpts=PTS-STARTPTS,format=rgba[timeline_base]")
        base_label = "[timeline_base]"
        for index, chunk in enumerate(chunks):
            if _is_audio_layer(chunk):
                # No frames to composite -- and `[N:v]` on a stream that does
                # not exist would fail the whole command.
                continue
            layer_graph, base_label = _timeline_layer_graph(
                chunk,
                input_index=index + 1,
                layer_index=index,
                base_label=base_label,
                scale_w=scale_w,
                scale_h=scale_h,
                frame_rate=frame_rate,
            )
            graph.extend(layer_graph)
        graph.append(f"{base_label}format=yuv420p[v]")

    audio_graph, audio_label = _timeline_audio_graph(chunks, base_has_audio=base_has_audio, duck=duck)
    graph.extend(audio_graph)

    if graph:
        command += ["-filter_complex", ";".join(graph)]
    command += ["-map", "[v]" if has_picture_layers else "0:v"]
    # Only re-encode audio when there is actually something to mix in; a pass
    # with nothing but silent layers keeps copying the base stream untouched.
    if audio_label:
        command += ["-map", audio_label]
    else:
        command += ["-map", "0:a?"]
    command += [
        "-c:v",
        *(
            ["libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p"]
            if has_picture_layers
            else ["copy"]
        ),
        "-c:a",
        *(["aac", "-b:a", "192k"] if audio_label else ["copy"]),
        "-movflags",
        "+faststart",
        str(output),
    ]
    return command


def _timeline_audio_mix_command(
    *,
    base_audio: Path,
    chunks: list[dict[str, Any]],
    output: Path,
    duck: bool = False,
) -> list[str]:
    """Mix audio-lane clips into a WAV that has no picture to composite onto.

    The MP4 path folds layer sound in while it composites; a WAV export (and
    an audio-only MP4 cut from a source with no video) never runs that pass,
    so without this an audio clip that plays in the editor is silent in every
    non-video export. The graph is the same one the compositor uses, which is
    what keeps the two formats sounding alike.
    """
    command = ["ffmpeg", "-y", "-i", str(base_audio)]
    for chunk in chunks:
        span = f"{max(0.04, float(chunk['outputEnd']) - float(chunk['outputStart'])):.6f}"
        command += [
            "-ss",
            f"{max(0.0, float(chunk['sourceSeek'])):.6f}",
            "-t",
            span,
            "-i",
            str(chunk["source"]),
        ]

    graph, audio_label = _timeline_audio_graph(chunks, base_has_audio=True, duck=duck)
    if not audio_label:
        return []
    command += [
        "-filter_complex",
        ";".join(graph),
        "-map",
        audio_label,
        "-acodec",
        "pcm_s16le",
        "-ar",
        "48000",
        "-ac",
        "2",
        str(output),
    ]
    return command


def _extend_base_timeline_command(
    *,
    base_video: Path,
    tail_duration: float,
    has_audio: bool,
    frame_rate: float,
    crf: int,
    output: Path,
) -> list[str]:
    """Grow the concatenated A-roll so appended clips have something to sit on.

    The base MP4 is exactly as long as what survived the cuts, because it is
    built from the source's own frames. Clips appended after it have no frames
    underneath them, and ffmpeg composites nothing onto nothing -- so the tail
    is black picture and silence of the right length, added here, once, before
    any layer is composited.

    `tpad`/`apad` rather than a generated clip and a concat: the codec
    parameters stay whatever the segments produced, which is the thing that
    makes stream-copy concatenation fragile in the first place.
    """
    filters = [
        f"[0:v]tpad=stop_mode=add:stop_duration={tail_duration:.6f}:color=black[v]",
    ]
    if has_audio:
        filters.append(f"[0:a]apad=pad_dur={tail_duration:.6f}[a]")
    command = ["ffmpeg", "-y", "-i", str(base_video)]
    command += [
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[v]",
    ]
    # A silent source stays silent; the layer mixer downstream copes with a
    # base that has no audio branch, and inventing one here would only be a
    # track of nothing.
    if has_audio:
        command += ["-map", "[a]"]
    command += [
        "-r",
        f"{max(1.0, frame_rate):.6f}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        *(["-c:a", "aac", "-b:a", "192k"] if has_audio else ["-an"]),
        "-movflags",
        "+faststart",
        str(output),
    ]
    return command


def _fps_filter_part(settings: dict[str, Any], source_video: str) -> str:
    """Return ffmpeg vf fragment e.g. ',fps=30' or ',fps=30000/1001' or empty."""
    fr = str(settings.get("frameRate") or "source").lower().strip()
    if fr in ("", "source"):
        probed = _ffprobe_avg_frame_rate(source_video)
        if probed:
            return f",fps={probed}"
        return ""
    if fr in ("24", "30", "60"):
        return f",fps={fr}"
    return ""


def _resolve_numeric_fps(settings: dict[str, Any], source_video: str) -> float:
    """Resolves the export frame rate as a float, for the matte renderer.

    Mirrors `_fps_filter_part`'s resolution logic (explicit rate, else probe
    the source, else fall back) but returns a number instead of an ffmpeg
    filter fragment, since `render_matte_video` needs a concrete fps to
    iterate frames by.
    """
    fr = str(settings.get("frameRate") or "source").lower().strip()
    if fr in ("24", "30", "60"):
        return float(fr)
    probed = _ffprobe_avg_frame_rate(source_video)
    if probed:
        try:
            if "/" in probed:
                num, den = probed.split("/", 1)
                den_f = float(den)
                if den_f:
                    return float(num) / den_f
            else:
                return float(probed)
        except (ValueError, ZeroDivisionError):
            pass
    return 30.0


def _remap_segments_to_export_timeline(
    segments: list[dict[str, Any]],
    normalized_ranges: list[tuple[float, float]],
    rates: list[float] | None = None,
) -> list[tuple[float, float, str]]:
    """Intersect segment timings with kept ranges; map to concatenated export time.

    `rates` (aligned with the ranges) compresses time inside a sped range:
    a caption over a 2x clip lands at half the offset and half the length.
    """
    out: list[tuple[float, float, str]] = []
    if not normalized_ranges:
        return out
    t_off = 0.0
    for range_index, (ks, ke) in enumerate(normalized_ranges):
        rate = rates[range_index] if rates and range_index < len(rates) else 1.0
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            try:
                s = float(seg.get("start", 0))
                e = float(seg.get("end", 0))
                text = str(seg.get("text") or "").strip()
            except (TypeError, ValueError):
                continue
            if not text or e <= s:
                continue
            a = max(s, ks)
            b = min(e, ke)
            if b - a < 0.02:
                continue
            out_s = t_off + (a - ks) / rate
            out_e = t_off + (b - ks) / rate
            out.append((out_s, out_e, text))
        t_off += (ke - ks) / rate
    out.sort(key=lambda x: x[0])
    return out


def _remap_segments_with_words(
    segments: list[dict[str, Any]],
    normalized_ranges: list[tuple[float, float]],
    rates: list[float] | None = None,
) -> list[dict[str, Any]]:
    """`_remap_segments_to_export_timeline`, keeping word timings.

    The styled (ASS) caption path needs per-word times for the karaoke
    highlight; the plain SRT path never did, so this is a sibling rather than
    a change of the tuple contract every existing test pins.
    """
    out: list[dict[str, Any]] = []
    if not normalized_ranges:
        return out
    t_off = 0.0
    for range_index, (ks, ke) in enumerate(normalized_ranges):
        rate = rates[range_index] if rates and range_index < len(rates) else 1.0
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            try:
                s = float(seg.get("start", 0))
                e = float(seg.get("end", 0))
                text = str(seg.get("text") or "").strip()
            except (TypeError, ValueError):
                continue
            if not text or e <= s:
                continue
            a = max(s, ks)
            b = min(e, ke)
            if b - a < 0.02:
                continue
            words: list[dict[str, Any]] = []
            for word in seg.get("words") or []:
                if not isinstance(word, dict):
                    continue
                try:
                    ws = float(word.get("start", -1))
                    we = float(word.get("end", -1))
                except (TypeError, ValueError):
                    continue
                token = str(word.get("word") or "").strip()
                if not token or we <= ws:
                    continue
                ws_clipped = max(ws, a)
                we_clipped = min(we, b)
                if we_clipped - ws_clipped < 0.01:
                    continue
                words.append(
                    {
                        "word": token,
                        "start": round(t_off + (ws_clipped - ks) / rate, 3),
                        "end": round(t_off + (we_clipped - ks) / rate, 3),
                    }
                )
            out.append(
                {
                    "start": round(t_off + (a - ks) / rate, 3),
                    "end": round(t_off + (b - ks) / rate, 3),
                    "text": text,
                    "words": words,
                }
            )
        t_off += (ke - ks) / rate
    out.sort(key=lambda item: item["start"])
    return out


def _trim_worded_around_layers(
    worded: list[dict[str, Any]],
    layers: list[tuple[float, float, str]],
) -> list[dict[str, Any]]:
    """The `_merge_subtitle_entries` trimming, applied to worded entries.

    Where an audible clip's own captions cover the A-roll, the A-roll entry is
    trimmed around them — and here its words are trimmed with it, so a karaoke
    sweep never runs across text that was cut out of the entry.
    """
    if not layers:
        return sorted(worded, key=lambda item: item["start"])
    kept: list[dict[str, Any]] = []
    for entry in worded:
        pieces = [(float(entry["start"]), float(entry["end"]))]
        for layer_start, layer_end, _ in layers:
            next_pieces: list[tuple[float, float]] = []
            for piece_start, piece_end in pieces:
                if layer_end <= piece_start or layer_start >= piece_end:
                    next_pieces.append((piece_start, piece_end))
                    continue
                if layer_start - piece_start > 0.25:
                    next_pieces.append((piece_start, layer_start))
                if piece_end - layer_end > 0.25:
                    next_pieces.append((layer_end, piece_end))
            pieces = next_pieces
        for piece_start, piece_end in pieces:
            words = [
                word
                for word in entry.get("words") or []
                if word["start"] < piece_end and word["end"] > piece_start
            ]
            kept.append(
                {
                    "start": piece_start,
                    "end": piece_end,
                    "text": entry.get("text") or "",
                    "words": words,
                }
            )
    return sorted(kept, key=lambda item: item["start"])


def _build_caption_burn_file(
    tmp_path: Path,
    *,
    plain_entries: list[tuple[float, float, str]],
    worded_entries: list[dict[str, Any]],
    layer_entries: list[tuple[float, float, str]],
    caption_style: dict[str, Any],
    scale_w: int,
    scale_h: int,
) -> tuple[Path, str, list[str]]:
    """The subtitle file to burn: styled ASS when a caption style was sent,
    the legacy bare SRT otherwise. Returns (path, renderer, warnings)."""
    if isinstance(caption_style, dict) and caption_style:
        from app.services.rough_cut_captions import build_ass

        entries: list[dict[str, Any]] = _trim_worded_around_layers(
            worded_entries, layer_entries
        )
        entries += [
            {"start": start, "end": end, "text": text, "words": []}
            for start, end, text in layer_entries
        ]
        entries.sort(key=lambda item: item["start"])
        script, warnings = build_ass(
            entries, caption_style, play_res_x=scale_w, play_res_y=scale_h
        )
        ass_path = tmp_path / "burnin.ass"
        ass_path.write_text(script, encoding="utf-8")
        if caption_style.get("fontFamily") and not os.environ.get("CAPTION_FONTS_DIR"):
            warnings.append(
                f"Caption font {caption_style.get('fontFamily')!r} renders with a system "
                "fallback unless it is installed on the export worker "
                "(set CAPTION_FONTS_DIR to a directory of font files)."
            )
        return ass_path, "ass", warnings

    srt_path = tmp_path / "burnin.srt"
    _write_srt(srt_path, plain_entries)
    return srt_path, "srt", []


def _remap_layer_segments_to_export_timeline(
    db: Session,
    chunks: list[dict[str, Any]],
) -> list[tuple[float, float, str]]:
    """Caption what an appended or overlaid clip is saying.

    Burn-in used to come only from the primary media's own transcription, which
    stops where the A-roll stops -- so a clip appended after it played with no
    captions at all, and the one placed there because of what is said in it was
    the one least able to afford that.

    Each clip's captions come from its own video's transcription, sliced to the
    part of its source the clip actually plays and shifted to where the clip
    sits on the output timeline.

    Only clips whose sound is playing are captioned. Subtitles transcribe what
    is audible; putting a muted B-roll shot's words on screen over somebody
    else's voice would be worse than leaving it bare.
    """
    entries: list[tuple[float, float, str]] = []
    by_video: dict[int, list[dict[str, Any]]] = {}

    for chunk in chunks:
        if not bool(chunk.get("audioEnabled")):
            continue
        # Generated media has no transcription row, and its `videoId` is the
        # primary's by legacy default -- captioning it would put the A-roll's
        # words under a shot that never said them.
        if chunk.get("sourceKind", "video") != "video":
            continue
        try:
            layer_video_id = int(chunk["videoId"])
            output_start = float(chunk["outputStart"])
            output_end = float(chunk["outputEnd"])
            # Where this chunk starts inside its OWN media. `sourceSeek` is
            # clip-relative once an effect has been baked, so it cannot be used
            # here -- the transcription is against the original.
            source_start = float(chunk["sourceStart"]) + float(chunk["clipOffset"])
        except (TypeError, ValueError, KeyError):
            continue
        span = output_end - output_start
        if span <= 0.02:
            continue
        source_end = source_start + span

        if layer_video_id not in by_video:
            by_video[layer_video_id] = _load_transcription_segments(db, layer_video_id)
        for segment in by_video[layer_video_id]:
            try:
                seg_start = float(segment.get("start", 0))
                seg_end = float(segment.get("end", 0))
                text = str(segment.get("text") or "").strip()
            except (TypeError, ValueError):
                continue
            if not text or seg_end <= seg_start:
                continue
            overlap_start = max(seg_start, source_start)
            overlap_end = min(seg_end, source_end)
            if overlap_end - overlap_start < 0.02:
                continue
            entries.append(
                (
                    output_start + (overlap_start - source_start),
                    output_start + (overlap_end - source_start),
                    text,
                )
            )

    entries.sort(key=lambda item: item[0])
    return entries


def _merge_subtitle_entries(
    base: list[tuple[float, float, str]],
    layers: list[tuple[float, float, str]],
) -> list[tuple[float, float, str]]:
    """Combine the A-roll's captions with the clips' own.

    Where an audible clip covers the A-roll both are playing, but two subtitles
    on screen at once reads as a fault rather than as detail. The clip placed on
    top is the one the viewer is being pointed at, so it takes the span: base
    entries are trimmed around it, and drop out entirely where it swallows them.
    """
    if not layers:
        return sorted(base, key=lambda item: item[0])

    kept: list[tuple[float, float, str]] = []
    for start, end, text in base:
        pieces = [(start, end)]
        for layer_start, layer_end, _ in layers:
            next_pieces: list[tuple[float, float]] = []
            for piece_start, piece_end in pieces:
                if layer_end <= piece_start or layer_start >= piece_end:
                    next_pieces.append((piece_start, piece_end))
                    continue
                if layer_start - piece_start > 0.25:
                    next_pieces.append((piece_start, layer_start))
                if piece_end - layer_end > 0.25:
                    next_pieces.append((layer_end, piece_end))
            pieces = next_pieces
        kept.extend((piece_start, piece_end, text) for piece_start, piece_end in pieces)

    return sorted([*kept, *layers], key=lambda item: item[0])


def _srt_timestamp(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000)) % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _write_srt(path: Path, entries: list[tuple[float, float, str]]) -> None:
    lines: list[str] = []
    for i, (start, end, text) in enumerate(entries, start=1):
        safe = re.sub(r"[\r\n]+", " ", text)
        lines.append(str(i))
        lines.append(f"{_srt_timestamp(start)} --> {_srt_timestamp(end)}")
        lines.append(safe)
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _escape_subtitles_filter_path(path: Path) -> str:
    # ffmpeg subtitles filter: escape special chars in path
    s = path.as_posix()
    s = s.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    return s


def _masks_for_segment(
    masks: list[Any], start: float, end: float, *, epsilon: float = 0.05
) -> list[Any]:
    """The masks that belong on THIS keep-range segment.

    The client stamps each mask with its clip's `sourceRange`; a mask whose
    stamped range shares an edge with the segment (the same drift tolerance as
    `_match_audio_range`) applies here, and `sourceRange` is stripped before
    the matte renderer sees the spec. Unstamped masks (legacy clients) apply
    to every segment — the old flattening behaviour, kept as the fallback
    rather than silently changing what an old request renders.
    """
    out: list[Any] = []
    for mask in masks:
        if not isinstance(mask, dict):
            out.append(mask)
            continue
        source_range = mask.get("sourceRange")
        if not isinstance(source_range, dict):
            out.append(mask)
            continue
        try:
            mask_start = float(source_range.get("start"))
            mask_end = float(source_range.get("end"))
        except (TypeError, ValueError):
            out.append({k: v for k, v in mask.items() if k != "sourceRange"})
            continue
        overlap = min(end, mask_end) - max(start, mask_start)
        edge = abs(mask_start - start) <= epsilon or abs(mask_end - end) <= epsilon
        if overlap > 0 and edge:
            out.append({k: v for k, v in mask.items() if k != "sourceRange"})
    return out



#: Per-clip speed bounds — mirror the inspector's slider (0.25x–4x).
_SPEED_MIN, _SPEED_MAX = 0.25, 4.0


def _clip_speed_rate(settings: dict[str, Any] | None) -> float:
    """The clip's speed multiplier from `videoRanges[].settings.speed.rate`."""
    if not isinstance(settings, dict):
        return 1.0
    speed = settings.get("speed")
    if not isinstance(speed, dict):
        return 1.0
    try:
        rate = float(speed.get("rate", 1.0))
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(rate) or abs(rate - 1.0) < 0.001:
        return 1.0
    return max(_SPEED_MIN, min(_SPEED_MAX, rate))


def _speed_rates_for_ranges(
    normalized: list[tuple[float, float]],
    *,
    video_ranges: dict[tuple[float, float], dict[str, Any]],
    masks: list[Any],
    processed_ranges: dict[tuple[float, float], Any],
) -> tuple[list[float], list[str]]:
    """Per-range playback rates, with the v1 support boundary enforced.

    Speed retimes a segment with `setpts`/`atempo`. That composes cleanly with
    the plain render path; it does NOT yet compose with the compositor's
    keyframe expressions (their `t` is source clock), a matte video (rendered
    at source cadence), or a processed effect source (already its own file) —
    so those clips render at 1x with a warning instead of rendering wrongly.
    The client applies the same veto before it stamps burn-in times, so the
    burn-in clock and the render agree by construction.
    """
    rates: list[float] = []
    warnings: list[str] = []
    for start, end in normalized:
        settings = _match_audio_range(video_ranges, start, end)
        rate = _clip_speed_rate(settings)
        if rate != 1.0 and (
            _needs_clip_compositor(settings)
            or _masks_for_segment(masks, start, end)
            or _match_audio_range(processed_ranges, start, end) is not None
        ):
            warnings.append(
                f"Speed on the clip at {start:.1f}s was not applied in the export "
                "(speed does not yet combine with masks, keyframed transforms, or "
                "rendered effects on the same clip)."
            )
            rate = 1.0
        rates.append(rate)
    return rates, warnings


def _output_span(start: float, end: float, rate: float) -> float:
    return max(0.0, end - start) / max(_SPEED_MIN, rate)


#: Output-profile loudness targets (integrated LUFS). Measured after the FULL
#: mix — the mix had no loudness handling at all before this; `loudnorm` lived
#: only inside the enhancement effect job (plan §5.3).
_LOUDNESS_TARGETS: dict[str, float] = {
    "youtube": -14.0,
    "social": -14.0,
    "podcast": -16.0,
    "broadcast": -23.0,
}


def _measure_loudness(input_path: Path, target_i: float) -> dict[str, str] | None:
    """Pass one of two-pass loudnorm: measure the final mix."""
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-y", "-i", str(input_path),
            "-af", f"loudnorm=I={target_i}:TP=-1.5:LRA=11:print_format=json",
            "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
        timeout=3600,
    )
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", result.stderr, re.DOTALL)
    if not match:
        return None
    try:
        measured = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    keys = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
    if not all(key in measured for key in keys):
        return None
    return {key: str(measured[key]) for key in keys}


def _normalize_loudness_command(
    input_path: Path,
    output_path: Path,
    target_i: float,
    measured: dict[str, str],
    *,
    has_video: bool,
) -> list[str]:
    """Pass two: linear gain to the target, video stream copied untouched —
    normalization must not cost a generation of picture quality."""
    loudnorm = (
        f"loudnorm=I={target_i}:TP=-1.5:LRA=11"
        f":measured_I={measured['input_i']}"
        f":measured_TP={measured['input_tp']}"
        f":measured_LRA={measured['input_lra']}"
        f":measured_thresh={measured['input_thresh']}"
        f":offset={measured['target_offset']}"
        ":linear=true"
    )
    if has_video:
        return [
            "ffmpeg", "-y", "-i", str(input_path),
            "-c:v", "copy",
            "-af", loudnorm,
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart",
            str(output_path),
        ]
    if str(output_path).lower().endswith(".wav"):
        return [
            "ffmpeg", "-y", "-i", str(input_path),
            "-af", loudnorm,
            "-acodec", "pcm_s16le", "-ar", "48000", "-ac", "2",
            str(output_path),
        ]
    return [
        "ffmpeg", "-y", "-i", str(input_path),
        "-af", loudnorm,
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(output_path),
    ]


def _apply_loudness_target(
    input_path: Path,
    tmp_path: Path,
    target: str,
    *,
    has_video: bool,
    suffix: str,
) -> tuple[Path, list[str]]:
    """Normalize the final mix to the named profile. Never fails the export:
    an unmeasurable file ships as-is with a warning."""
    target_i = _LOUDNESS_TARGETS.get(target)
    if target_i is None:
        return input_path, []
    measured = _measure_loudness(input_path, target_i)
    if measured is None:
        return input_path, [
            f"Loudness normalization ({target}) was skipped: the mix could not be measured."
        ]
    output = tmp_path / f"loudnorm_{suffix}"
    _run_ffmpeg(
        _normalize_loudness_command(
            input_path, output, target_i, measured, has_video=has_video
        )
    )
    return output, []


def _sanitize_burn_ins(
    raw: Any,
    *,
    tmp_path: Path,
    output_duration: float,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Decode client-rasterized overlay frames into files ffmpeg can open.

    Each entry is a full-frame transparent PNG the browser already positioned
    and composited, plus the output-timeline span it is on screen for -- the
    same post-cut axis `_write_srt` entries use, so a burn-in and a caption
    quoting the same moment agree.

    Everything here is hostile input: the base64 must decode, the bytes must
    actually be a PNG, and the span must be finite, forward, and inside the
    render. Anything that fails is dropped with a reason rather than failing
    the export -- one bad title should not cost the user the whole render.
    """
    entries: list[dict[str, Any]] = []
    skipped: list[str] = []
    if not isinstance(raw, list) or not raw:
        return entries, skipped
    if output_duration <= 0:
        return entries, [f"burnIn:noOutputDuration x{len(raw)}"]

    for index, item in enumerate(raw):
        if len(entries) >= MAX_BURN_INS:
            skipped.append(f"burnIn:limit({MAX_BURN_INS})")
            break
        if not isinstance(item, dict):
            skipped.append(f"burnIn[{index}]:malformed")
            continue

        png = item.get("png")
        if not isinstance(png, str) or not png.strip():
            skipped.append(f"burnIn[{index}]:missingPng")
            continue
        encoded = png.strip()
        # Tolerate a data: URL even though the contract asks for bare base64 --
        # cheaper than a render that silently drops every overlay.
        if encoded.startswith("data:"):
            _, _, encoded = encoded.partition(",")
        # Measure before decoding: base64 carries 3 bytes per 4 characters, so
        # an oversized blob can be rejected without materializing it.
        if (len(encoded) * 3) // 4 > MAX_BURN_IN_PNG_BYTES:
            skipped.append(f"burnIn[{index}]:tooLarge")
            continue
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            skipped.append(f"burnIn[{index}]:badBase64")
            continue
        if len(data) > MAX_BURN_IN_PNG_BYTES:
            skipped.append(f"burnIn[{index}]:tooLarge")
            continue
        if not data.startswith(_PNG_MAGIC):
            skipped.append(f"burnIn[{index}]:notPng")
            continue

        try:
            start = float(item.get("start", 0.0))
            end = float(item.get("end", 0.0))
        except (TypeError, ValueError):
            skipped.append(f"burnIn[{index}]:badSpan")
            continue
        if not math.isfinite(start) or not math.isfinite(end):
            skipped.append(f"burnIn[{index}]:badSpan")
            continue
        if start < 0 or end <= start:
            skipped.append(f"burnIn[{index}]:badSpan")
            continue
        start = min(start, output_duration)
        end = min(end, output_duration)
        if end - start < MIN_BURN_IN_SECONDS:
            skipped.append(f"burnIn[{index}]:outsideOutput")
            continue

        span = end - start
        fades: list[float] = []
        for key in ("fadeIn", "fadeOut"):
            try:
                value = float(item.get(key, 0.0) or 0.0)
            except (TypeError, ValueError):
                value = 0.0
            if not math.isfinite(value) or value <= 0:
                value = 0.0
            # A fade longer than half the span would meet the other one and
            # the overlay would never reach full opacity.
            fades.append(min(value, span / 2.0))

        motion = _sanitize_burn_in_motion(item.get("motion"), index, skipped)

        path = tmp_path / f"burn_in_{len(entries)}.png"
        path.write_bytes(data)
        entries.append(
            {
                "path": path,
                "start": start,
                "end": end,
                "fadeIn": fades[0],
                "fadeOut": fades[1],
                "motion": motion,
            }
        )

    return entries, skipped


#: The text engine's own preset vocabulary (`animationIn`/`animationOut` in
#: `rough-cut-types.ts`), NOT the timeline-layer preset names -- the two are
#: different curves in the editor and stay different here.
_TEXT_MOTION_PRESETS = {"none", "fade", "slide", "rise", "pop", "spin", "twist"}

#: Presets whose motion is a transform about the block's own centre. Without a
#: bounding box there is no pivot: scaling the full-frame raster about the
#: frame centre would swing an off-centre title across the picture.
_PIVOT_MOTION_PRESETS = {"pop", "spin", "twist"}


def _sanitize_burn_in_motion(
    raw: Any, index: int, skipped: list[str]
) -> dict[str, Any] | None:
    """Validate a burn-in's optional text-motion block.

    The client sends the overlay's own animation settings -- preset names from
    the text engine, the duration the user chose, the resolved em (frame px,
    block scale included) the offsets are proportional to, and the block's
    bounding box so transforms pivot about the block rather than the frame.
    Hostile or missing pieces degrade one channel at a time with a reason,
    never the whole entry: a title with a broken box still fades.
    """
    if not isinstance(raw, dict):
        return None
    preset_in = str(raw.get("in") or "none")
    box = _sanitize_motion_box(raw.get("box"))
    frame = _sanitize_motion_frame(raw.get("frame"))

    if isinstance(raw.get("phases"), list):
        phases = _sanitize_motion_phases(raw["phases"], index, skipped, has_box=box is not None)
        if not phases:
            return None
        return {"phases": phases, "box": box, "frame": frame}

    preset_out = str(raw.get("out") or "none")
    if preset_in not in _TEXT_MOTION_PRESETS:
        preset_in = "none"
    if preset_out not in _TEXT_MOTION_PRESETS:
        preset_out = "none"
    if preset_in == "none" and preset_out == "none":
        return None

    duration = _number_between(raw.get("duration"), 0.02, 3.0, 0.45)
    em = _number_between(raw.get("em"), 4.0, 400.0, 32.0)

    if box is None:
        downgraded = False
        if preset_in in _PIVOT_MOTION_PRESETS:
            preset_in, downgraded = "fade", True
        if preset_out in _PIVOT_MOTION_PRESETS:
            preset_out, downgraded = "fade", True
        if downgraded:
            skipped.append(f"burnIn[{index}]:motionPivotMissing")

    return {
        "in": preset_in,
        "out": preset_out,
        "duration": duration,
        "em": em,
        "box": box,
        "frame": frame,
    }


def _sanitize_motion_box(raw_box: Any) -> dict[str, float] | None:
    if not isinstance(raw_box, dict):
        return None
    try:
        values = [float(raw_box.get(key, 0.0)) for key in ("x", "y", "width", "height")]
    except (TypeError, ValueError):
        return None
    if (
        len(values) == 4
        and all(math.isfinite(value) for value in values)
        and values[2] >= 4
        and values[3] >= 4
    ):
        return {"x": values[0], "y": values[1], "width": values[2], "height": values[3]}
    return None


def _sanitize_motion_frame(raw_frame: Any) -> dict[str, float] | None:
    if not isinstance(raw_frame, dict):
        return None
    try:
        frame_w = float(raw_frame.get("width", 0.0))
        frame_h = float(raw_frame.get("height", 0.0))
    except (TypeError, ValueError):
        return None
    if math.isfinite(frame_w) and math.isfinite(frame_h) and frame_w >= 2 and frame_h >= 2:
        return {"width": frame_w, "height": frame_h}
    return None


#: Sampled-track channels, with the range each value is clamped into. `dx`/`dy`
#: are frame px, `rotation` degrees, `scale`/`opacity` factors -- the same
#: units the preset path emits, so `_append_motion_burn_in` serves both.
_MOTION_TRACK_LIMITS: dict[str, tuple[float, float]] = {
    "dx": (-8192.0, 8192.0),
    "dy": (-8192.0, 8192.0),
    "scale": (0.01, 6.0),
    "rotation": (-3600.0, 3600.0),
    "opacity": (0.0, 1.5),
}
_MOTION_PHASE_ANCHORS = {"in", "out", "loop"}
_MOTION_MAX_SAMPLES = 120


def _sanitize_motion_phases(
    raw_phases: list[Any], index: int, skipped: list[str], *, has_box: bool
) -> list[dict[str, Any]]:
    """Validate the sampled-track motion form.

    This is the generic channel: the client samples its OWN animation engine
    (whatever curves, easings and intensities it runs) into piecewise-linear
    tracks, and the render replays them -- no preset vocabulary to keep in
    sync, exact by construction at the sampled density. One phase per anchor:
    `in` plays from the overlay's entrance, `out` ends at its exit, `loop`
    wraps `mod(t-start, duration)` for the whole span. Without a pivot box the
    scale/rotation tracks are dropped (translate and opacity are pivot-free),
    one channel at a time, with a reason.
    """
    phases: list[dict[str, Any]] = []
    seen_anchors: set[str] = set()
    dropped_pivot = False
    for raw_phase in raw_phases[:4]:
        if not isinstance(raw_phase, dict):
            continue
        anchor = str(raw_phase.get("at") or "")
        if anchor not in _MOTION_PHASE_ANCHORS or anchor in seen_anchors:
            continue
        try:
            duration_raw = float(raw_phase.get("duration"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(duration_raw) or duration_raw <= 0:
            continue
        duration = max(0.02, min(6.0, duration_raw))
        raw_tracks = raw_phase.get("tracks")
        if not isinstance(raw_tracks, dict):
            continue
        tracks: dict[str, list[dict[str, float]]] = {}
        for key, (low, high) in _MOTION_TRACK_LIMITS.items():
            samples = raw_tracks.get(key)
            if not isinstance(samples, list) or len(samples) < 2:
                continue
            if key in {"scale", "rotation"} and not has_box:
                dropped_pivot = True
                continue
            clean: list[dict[str, float]] = []
            for sample in samples[:_MOTION_MAX_SAMPLES]:
                if not isinstance(sample, dict):
                    clean = []
                    break
                try:
                    t = float(sample.get("t", 0.0))
                    v = float(sample.get("v", 0.0))
                except (TypeError, ValueError):
                    clean = []
                    break
                if not (math.isfinite(t) and math.isfinite(v)):
                    clean = []
                    break
                clean.append({"t": max(0.0, min(t, duration)), "v": max(low, min(high, v))})
            if len(clean) >= 2:
                clean.sort(key=lambda item: item["t"])
                tracks[key] = clean
        if tracks:
            seen_anchors.add(anchor)
            phases.append({"at": anchor, "duration": duration, "tracks": tracks})
    if dropped_pivot:
        skipped.append(f"burnIn[{index}]:motionPivotMissing")
    if not phases and raw_phases:
        skipped.append(f"burnIn[{index}]:motionInvalid")
    return phases


def _eased_out_cubic01(progress: str) -> str:
    """Clamp to 0-1 then cubic ease-out -- `easeOut` in text-canvas-animation.ts."""
    clamped = f"max(0,min(1,{progress}))"
    return f"(1-pow(1-({clamped}),3))"


def _text_motion_channels(
    motion: dict[str, Any], start: float, end: float, time_var: str = "t"
) -> dict[str, str]:
    """Per-channel ffmpeg expressions mirroring `textUnitTransform`.

    The viewer's enter/exit transforms, reproduced curve for curve: slide is
    0.45em, rise enters by 0.4em and leaves by 0.3em, pop overshoots
    0.82->1.04->1, spin is 12 degrees with a 0.9->1 scale, twist is a cos()
    foreshortening standing in for rotateY. Exit wins over enter -- an overlay
    shorter than twice its animation duration must be seen to go -- so the
    exit window is the OUTER if(), exactly as `textAnimationState` orders it.
    """
    duration = float(motion["duration"])
    em = float(motion["em"])
    settled = {"dx": "0", "dy": "0", "scale": "1", "scaleX": "1", "rot": "0", "opacity": "1"}

    def _enter() -> dict[str, str]:
        eased = _eased_out_cubic01(f"(({time_var})-{start:.6f})/{duration:.6f}")
        away = f"(1-{eased})"
        channels = dict(settled)
        preset = motion["in"]
        if preset == "fade":
            channels["opacity"] = eased
        elif preset == "slide":
            channels["dx"] = f"({away})*{em * 0.45:.6f}"
            channels["opacity"] = eased
        elif preset == "rise":
            channels["dy"] = f"({away})*{em * 0.4:.6f}"
            channels["opacity"] = eased
        elif preset == "pop":
            channels["scale"] = (
                f"if(lt({eased},0.7),0.82+(({eased})/0.7)*0.22,"
                f"1.04-((({eased})-0.7)/0.3)*0.04)"
            )
            channels["opacity"] = eased
        elif preset == "spin":
            channels["rot"] = f"-12*({away})"
            channels["scale"] = f"(0.9+({eased})*0.1)"
            channels["opacity"] = eased
        elif preset == "twist":
            channels["scaleX"] = f"cos(34*({away})*PI/180)"
            channels["opacity"] = eased
        return channels

    def _exit() -> dict[str, str]:
        eased = _eased_out_cubic01(f"1-({end:.6f}-({time_var}))/{duration:.6f}")
        channels = dict(settled)
        preset = motion["out"]
        if preset == "fade":
            channels["opacity"] = f"(1-{eased})"
        elif preset == "slide":
            channels["dx"] = f"({eased})*{em * 0.45:.6f}"
            channels["opacity"] = f"(1-{eased})"
        elif preset == "rise":
            channels["dy"] = f"-({eased})*{em * 0.3:.6f}"
            channels["opacity"] = f"(1-{eased})"
        elif preset == "pop":
            channels["scale"] = f"(1-({eased})*0.18)"
            channels["opacity"] = f"(1-{eased})"
        elif preset == "spin":
            channels["rot"] = f"12*({eased})"
            channels["scale"] = f"(1-({eased})*0.1)"
            channels["opacity"] = f"(1-{eased})"
        elif preset == "twist":
            channels["scaleX"] = f"cos(34*({eased})*PI/180)"
            channels["opacity"] = f"(1-{eased})"
        return channels

    entered = _enter() if motion["in"] != "none" else None
    exited = _exit() if motion["out"] != "none" else None
    result: dict[str, str] = {}
    for key, base in settled.items():
        # A channel neither phase touches stays its literal identity, so the
        # graph builder can tell "no scale anywhere" from "scale that happens
        # to be 1 right now" and skip the whole filter.
        expression = base
        if entered is not None and entered[key] != base:
            expression = (
                f"if(lte(({time_var})-{start:.6f},{duration:.6f}),{entered[key]},{base})"
            )
        if exited is not None and exited[key] != base:
            expression = (
                f"if(lte({end:.6f}-({time_var}),{duration:.6f}),{exited[key]},{expression})"
            )
        result[key] = expression
    return result


def _sampled_track_expression(samples: list[dict[str, float]], local: str) -> str:
    """Piecewise-linear interpolation over a phase-local time expression.

    The easing is already baked into the sample values by the client's own
    engine, so linear segments between dense samples reproduce any curve it
    can play -- the same shape `_channel_expression` builds for keyframes.
    """
    expression = f"{samples[-1]['v']:.6f}"
    for index in range(len(samples) - 2, -1, -1):
        start = samples[index]
        end = samples[index + 1]
        span = max(0.0005, end["t"] - start["t"])
        interpolated = (
            f"{start['v']:.6f}+({end['v'] - start['v']:.6f})*"
            f"(({local})-{start['t']:.6f})/{span:.6f}"
        )
        expression = f"if(lt(({local}),{end['t']:.6f}),{interpolated},{expression})"
    return f"if(lte(({local}),{samples[0]['t']:.6f}),{samples[0]['v']:.6f},{expression})"


#: Sampled track key -> the channel name `_append_motion_burn_in` consumes.
_MOTION_TRACK_CHANNEL = {
    "dx": "dx",
    "dy": "dy",
    "scale": "scale",
    "rotation": "rot",
    "opacity": "opacity",
}


def _sampled_motion_channels(
    motion: dict[str, Any], start: float, end: float, time_var: str = "t"
) -> dict[str, str]:
    """Channels from sampled phases -- the client engine's own curves, replayed.

    `in` runs on time-from-entrance and holds its last value outside its
    window via the phase gate; `out` runs on time-into-the-exit-window ending
    at the overlay's exit; `loop` wraps for the whole span. Phases combine the
    way the element engine's `merge` does: opacities and scales multiply,
    offsets and rotations add.
    """
    settled = {"dx": "0", "dy": "0", "scale": "1", "scaleX": "1", "rot": "0", "opacity": "1"}
    result = dict(settled)
    for phase in motion.get("phases") or []:
        duration = float(phase["duration"])
        anchor = phase["at"]
        if anchor == "in":
            local = f"(({time_var})-{start:.6f})"
            active = f"lte(({time_var})-{start:.6f},{duration:.6f})"
        elif anchor == "out":
            begin = end - duration
            local = f"(({time_var})-{begin:.6f})"
            active = f"gte(({time_var}),{begin:.6f})"
        else:
            local = f"mod(({time_var})-{start:.6f},{duration:.6f})"
            active = None
        for key, samples in phase["tracks"].items():
            channel = _MOTION_TRACK_CHANNEL[key]
            identity = "1" if channel in {"scale", "opacity"} else "0"
            expression = _sampled_track_expression(samples, local)
            if active is not None:
                expression = f"if({active},{expression},{identity})"
            if channel in {"scale", "opacity"}:
                result[channel] = (
                    expression
                    if result[channel] == "1"
                    else f"({result[channel]})*({expression})"
                )
            else:
                result[channel] = (
                    expression
                    if result[channel] == "0"
                    else f"({result[channel]})+({expression})"
                )
    return result


def _burn_in_motion_channels(
    motion: dict[str, Any], start: float, end: float, time_var: str = "t"
) -> dict[str, str]:
    if motion.get("phases"):
        return _sampled_motion_channels(motion, start, end, time_var)
    return _text_motion_channels(motion, start, end, time_var)


def _append_motion_burn_in(
    graph: list[str],
    *,
    index: int,
    item: dict[str, Any],
    width: int,
    height: int,
    base_label: str,
    out_label: str,
) -> None:
    """One animated burn-in's filter chain -- the compositor's own moves.

    The looped PNG stream's clock is the OUTPUT clock (its `-t` bound is the
    overlay's exit, not its span), so every expression can use `t` directly.
    With a bounding box, the raster is cropped to the block and animated about
    the block's own centre: geq ramps alpha (pixel time `T`), rotate spins on
    an enlarged transparent canvas, `scale ... eval=frame` breathes, and the
    overlay places the animated centre back where the block lives, plus the
    slide/rise offset. Without a box only the pivot-free channels run
    (translate + opacity) on the full frame -- `_sanitize_burn_in_motion`
    already downgraded the rest. Motion owns opacity: the static path's
    fade filters do not apply here, so a client that sent both cannot fade
    the same alpha twice.
    """
    motion = item["motion"]
    start = float(item["start"])
    end = float(item["end"])
    channels = _burn_in_motion_channels(motion, start, end)
    filters = ["format=rgba", f"scale={width}:{height}"]

    box = motion.get("box")
    if box:
        frame = motion.get("frame")
        scale_x = width / frame["width"] if frame else 1.0
        scale_y = height / frame["height"] if frame else 1.0
        box_x = max(0.0, min(box["x"] * scale_x, width - 4.0))
        box_y = max(0.0, min(box["y"] * scale_y, height - 4.0))
        box_w = max(4.0, min(box["width"] * scale_x, width - box_x))
        box_h = max(4.0, min(box["height"] * scale_y, height - box_y))
        crop_w = max(2, int(round(box_w / 2)) * 2)
        crop_h = max(2, int(round(box_h / 2)) * 2)
        crop_x = min(int(round(box_x)), width - crop_w)
        crop_y = min(int(round(box_y)), height - crop_h)
        filters.append(f"crop={crop_w}:{crop_h}:{max(0, crop_x)}:{max(0, crop_y)}")
        centre_x = max(0, crop_x) + crop_w / 2.0
        centre_y = max(0, crop_y) + crop_h / 2.0
        base_w, base_h = crop_w, crop_h
    else:
        centre_x = width / 2.0
        centre_y = height / 2.0
        base_w, base_h = width, height

    if channels["opacity"] != "1":
        opacity_at_pixel_time = re.sub(r"\bt\b", "T", channels["opacity"])
        filters.append(
            "geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
            f"a='alpha(X,Y)*({opacity_at_pixel_time})'"
        )

    layer = f"burn_layer{index}"
    graph.append(f"[{index + 1}:v]{','.join(filters)}[{layer}]")
    current = layer

    if channels["rot"] != "0":
        diagonal = max(2, int(math.ceil(math.hypot(base_w, base_h) / 2) * 2))
        graph.append(
            f"[{current}]rotate=angle='({channels['rot']})*PI/180':"
            f"ow={diagonal}:oh={diagonal}:c=none[{layer}_rot]"
        )
        current = f"{layer}_rot"
        base_w = base_h = diagonal

    if channels["scale"] != "1" or channels["scaleX"] != "1":
        scale_x_expression = f"({channels['scale']})*({channels['scaleX']})"
        graph.append(
            f"[{current}]scale="
            f"w='trunc(max(2,{base_w}*({scale_x_expression}))/2)*2':"
            f"h='trunc(max(2,{base_h}*({channels['scale']}))/2)*2':eval=frame[{layer}_scaled]"
        )
        current = f"{layer}_scaled"

    overlay_x = f"{centre_x:.4f}-w/2+({channels['dx']})"
    overlay_y = f"{centre_y:.4f}-h/2+({channels['dy']})"
    graph.append(
        f"[{base_label}][{current}]overlay=x='{overlay_x}':y='{overlay_y}':format=auto:"
        f"enable='between(t,{start:.6f},{end:.6f})'[{out_label}]"
    )


def _burn_in_overlay_command(
    *,
    base_video: Path,
    burn_ins: list[dict[str, Any]],
    width: int,
    height: int,
    frame_rate: float,
    crf: int,
    output: Path,
) -> list[str]:
    """Composite every rasterized overlay frame onto the render in one pass.

    The PNGs arrive full-frame at the export resolution, so they land at 0:0
    untouched. `scale` is still there because a client that rasterized against
    a stale resolution would otherwise overlay a mismatched rectangle in the
    corner, which is worse than a resample.

    Each image is looped rather than read as a single frame: `fade` needs a
    stream with frames at the relevant timestamps to ramp alpha across, and a
    one-frame input has nothing to ramp. `-t` bounds the loop at the moment the
    overlay leaves the screen so nothing is decoded past its span.
    """
    fps = max(1.0, float(frame_rate))
    command = ["ffmpeg", "-y", "-i", str(base_video)]
    for item in burn_ins:
        command += [
            "-loop",
            "1",
            "-framerate",
            f"{fps:.6f}",
            "-t",
            f"{float(item['end']):.6f}",
            "-i",
            str(item["path"]),
        ]

    graph = ["[0:v]setpts=PTS-STARTPTS,format=rgba[burn_base0]"]
    current = "burn_base0"
    for index, item in enumerate(burn_ins):
        start = float(item["start"])
        end = float(item["end"])
        nxt = f"burn_base{index + 1}"
        if item.get("motion"):
            _append_motion_burn_in(
                graph,
                index=index,
                item=item,
                width=int(width),
                height=int(height),
                base_label=current,
                out_label=nxt,
            )
            current = nxt
            continue
        fade_in = float(item.get("fadeIn") or 0.0)
        fade_out = float(item.get("fadeOut") or 0.0)
        filters = ["format=rgba", f"scale={int(width)}:{int(height)}"]
        if fade_in > 0:
            filters.append(f"fade=t=in:st={start:.6f}:d={fade_in:.6f}:alpha=1")
        if fade_out > 0:
            fade_out_start = max(start, end - fade_out)
            filters.append(f"fade=t=out:st={fade_out_start:.6f}:d={fade_out:.6f}:alpha=1")
        layer = f"burn_layer{index}"
        graph.append(f"[{index + 1}:v]{','.join(filters)}[{layer}]")
        graph.append(
            f"[{current}][{layer}]overlay=0:0:format=auto:"
            f"enable='between(t,{start:.6f},{end:.6f})'[{nxt}]"
        )
        current = nxt
    graph.append(f"[{current}]format=yuv420p[v]")

    command += [
        "-filter_complex",
        ";".join(graph),
        "-map",
        "[v]",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(output),
    ]
    return command


def _load_transcription_segments(db: Session, video_id: int) -> list[dict[str, Any]]:
    row = db.query(VideoTranscription).filter(VideoTranscription.video_id == video_id).first()
    if not row or not row.segments:
        return []
    raw = row.segments
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            out.append(item)
    return out


def rough_cut_export_job(ai_result_id: int, register_as_version: bool = False) -> None:
    db: Session = SessionLocal()
    try:
        row = db.query(AiResult).filter(AiResult.id == ai_result_id).first()
        if row is None or row.result_type != "rough_cut_export":
            raise RuntimeError(f"Rough-cut export {ai_result_id} was removed before processing")

        payload = dict(row.result_data or {})
        # Burn-in frames are an INPUT, not a result. Left on the row they were
        # re-serialized on every progress commit and re-sent on every 850ms
        # poll of this job -- megabytes of base64 per tick, for the life of the
        # render. Taken off here, before the first write-back, and decoded to
        # files below.
        raw_burn_ins = payload.pop("burnIns", None)
        raw_burn_ins = raw_burn_ins if isinstance(raw_burn_ins, list) else []
        video_id = row.video_id
        fmt = str(payload.get("format") or "mp4").lower()

        video = db.query(Video).filter(Video.id == video_id).first()
        media_src = (video.file_path if video else "") or ""
        # I8: clamp keep-ranges against the real source duration before
        # anything downstream (matte rendering, ffmpeg -t) can be pinned by
        # a hostile/malformed range extending far past the actual media.
        source_duration = _ffprobe_duration(media_src) if media_src else None
        normalized = _normalize_ranges(payload.get("keepRanges"), max_end=source_duration)
        transitions = _normalize_transitions(payload.get("transitions"), normalized)
        # Adjacent between-cut transitions get a REAL audio crossfade at the
        # WAV merge, replacing the old fade-out-then-fade-in dip to silence.
        audio_crossfades = _crossfade_boundaries(transitions, len(normalized))

        row.status = "processing"
        row.error_message = None
        payload = _merge_result_payload(payload, {"progress": 3})
        row.result_data = payload
        db.commit()

        if not normalized:
            raise ValueError("No valid segments to export (check keep ranges)")
        if not media_src.strip():
            raise ValueError("Source video path is missing on the server")

        settings = payload.get("exportSettings") if isinstance(payload.get("exportSettings"), dict) else {}
        quality = str(settings.get("quality") or "standard")
        resolution = str(settings.get("resolution") or "1080p")
        crf = {"draft": 28, "standard": 23, "high": 18}.get(quality, 23)
        scale = {"720p": "1280:720", "1080p": "1920:1080", "4k": "3840:2160"}.get(resolution, "1920:1080")

        include_captions = bool(settings.get("includeCaptions", True))
        # Music/audio-lane ducking under the programme's own voice, and the
        # final-mix loudness target — both export-settings-driven (plan §7.7).
        duck_music = bool(settings.get("duckMusic"))
        loudness_target = str(settings.get("loudnessTarget") or "off").strip().lower()
        include_lt = bool(settings.get("includeLowerThirds", False))
        include_brand = bool(settings.get("includeBrand", False))
        shorts_export = bool(settings.get("shortsExport") or settings.get("verticalExport"))

        has_video = _ffprobe_has_video(media_src)
        want_mp4_video = fmt == "mp4" and has_video

        burn_in_entries: list[dict[str, Any]] = []

        burn_skipped: list[str] = []
        # "lowerThirds"/"brand" report overlays this worker cannot draw itself.
        # A client that rasterized them says so explicitly, and those cards are
        # in `burnIns` below -- reporting them as skipped would then be a lie.
        # Anything else (an older client) is still genuinely absent.
        client_burned_lower_thirds = bool(
            payload.get("burnInsIncludeLowerThirds") and raw_burn_ins and want_mp4_video
        )
        if include_lt and not client_burned_lower_thirds:
            burn_skipped.append("lowerThirds")
        client_burned_brand = bool(
            payload.get("burnInsIncludeBrand") and raw_burn_ins and want_mp4_video
        )
        if include_brand and not client_burned_brand:
            burn_skipped.append("brand")
        try:
            rejected_at_ingest = int(payload.get("burnInsRejected") or 0)
        except (TypeError, ValueError):
            rejected_at_ingest = 0
        if rejected_at_ingest > 0:
            burn_skipped.append(f"burnIn:rejectedAtIngest x{rejected_at_ingest}")

        if not cloudinary_credentials_configured():
            raise RuntimeError("Cloudinary credentials are required to finish exports (CLOUDINARY_* env).")

        fps_extra = _fps_filter_part(settings, media_src) if want_mp4_video else ""
        masks = payload.get("masks") if isinstance(payload.get("masks"), list) else []
        processed_ranges = _approved_processed_ranges(
            db, video_id, payload.get("processedRanges")
        )
        caption_warnings: list[str] = []
        color_ranges = _range_settings(payload.get("colorRanges"))
        video_ranges = _range_settings(payload.get("videoRanges"))
        # LUT references become rendered cube paths before any chain is built.
        # In place on purpose: the overlay chunks alias these same settings
        # dicts, so resolving the map resolves the layers too.
        lut_refs = [s for s in color_ranges.values() if isinstance(s, dict)]
        lut_refs += [
            s["adjust"] for s in video_ranges.values()
            if isinstance(s, dict) and isinstance(s.get("adjust"), dict)
        ]
        if any(ref.get("lut") for ref in lut_refs):
            from app.services.lut import resolve_adjust_lut, video_workspace_ids

            allowed = video_workspace_ids(db, video)
            for ref in lut_refs:
                if ref.get("lut"):
                    resolve_adjust_lut(db, ref, allowed_workspace_ids=allowed)
        # Per-range speed, with the v1 support veto. The client applies the
        # same veto before stamping burn-in times, so both clocks agree.
        range_rates, speed_warnings = _speed_rates_for_ranges(
            normalized,
            video_ranges=video_ranges,
            masks=masks,
            processed_ranges=processed_ranges,
        )
        caption_warnings.extend(speed_warnings)
        audio_ranges = _audio_range_settings(payload.get("audioRanges"))
        enhanced_audio_ranges = _approved_audio_ranges(
            db, video_id, payload.get("audioRanges")
        )
        muted_ranges = _muted_source_ranges(payload.get("mutedRanges"))
        # Audio-lane clips have no picture, so they are resolved whatever the
        # format: gating every timeline layer behind `want_mp4_video` is what
        # made a music bed audible in the editor and silent in a WAV export.
        timeline_layers = _approved_timeline_layers(
            db,
            video_id,
            payload.get("timelineLayers"),
            media_src=media_src,
            source_duration=source_duration,
        )
        # LUTs on timeline layers resolve through the same authorised path as
        # the A-roll's. They used to be skipped entirely: the layer kept its
        # unresolved `{assetId, workspaceId}` reference, `build_*_adjust_*`
        # found no `path`, and a B-roll LUT that previewed in the browser was
        # silently absent from the MP4 (plan §5.3 #6). In place on purpose —
        # the export chunks `{**layer}`-spread these same nested dicts.
        layer_lut_refs = [
            layer["settings"]["adjust"]
            for layer in timeline_layers
            if isinstance(layer.get("settings"), dict)
            and isinstance(layer["settings"].get("adjust"), dict)
            and layer["settings"]["adjust"].get("lut")
        ]
        if layer_lut_refs:
            from app.services.lut import resolve_adjust_lut, video_workspace_ids

            allowed_layer_ws = video_workspace_ids(db, video)
            for ref in layer_lut_refs:
                resolve_adjust_lut(db, ref, allowed_workspace_ids=allowed_layer_ws)
        if not want_mp4_video:
            timeline_layers = [
                layer for layer in timeline_layers if str(layer.get("kind")) == "audio"
            ]
        timeline_chunks = _remap_timeline_layers_to_export(
            timeline_layers,
            normalized,
            source_duration=source_duration,
            rates=range_rates,
        )
        # An audio lane never reaches a compositing pass as picture; it is fed
        # to the mixer alone, so it must be held apart from both text passes.
        audio_lane_chunks = [chunk for chunk in timeline_chunks if _is_audio_layer(chunk)]
        picture_chunks = [chunk for chunk in timeline_chunks if not _is_audio_layer(chunk)]
        below_text_chunks = [chunk for chunk in picture_chunks if not bool(chunk.get("aboveText"))]
        above_text_chunks = [chunk for chunk in picture_chunks if bool(chunk.get("aboveText"))]
        # How much timeline sits past the end of the A-roll. The base MP4 is
        # built from the source's own frames and so stops where they do; this
        # is the black-and-silence the appended clips need underneath them.
        kept_total = sum(
            _output_span(start, end, range_rates[index])
            for index, (start, end) in enumerate(normalized)
        )
        timeline_tail = min(
            MAX_TAIL_SECONDS,
            max(
                0.0,
                max((float(chunk["outputEnd"]) for chunk in timeline_chunks), default=0.0)
                - kept_total,
            ),
        )
        if timeline_tail > 0.02:
            # Everything downstream of the concat measures against the extended
            # timeline; drop any chunk the cap pushed off the end so nothing is
            # composited past where the picture stops.
            limit = kept_total + timeline_tail
            below_text_chunks = [c for c in below_text_chunks if float(c["outputStart"]) < limit - 0.02]
            above_text_chunks = [c for c in above_text_chunks if float(c["outputStart"]) < limit - 0.02]
            audio_lane_chunks = [c for c in audio_lane_chunks if float(c["outputStart"]) < limit - 0.02]
        # I11: tracked across segments -- if a mask was requested but any
        # segment's matte failed to render, the export still finishes
        # (fail-open: a masking bug must not fail an entire export) but the
        # UI needs to know the result is unmasked, since a mask can be a
        # redaction, not just decoration.
        mask_render_failed_for_segment = False
        # Text masks may name a font this worker does not ship; the vendored
        # default is substituted so the export still renders, and the id is
        # collected here so the result can say so instead of silently
        # shipping a different typeface than the editor previewed.
        mask_font_fallbacks: set[str] = set()
        render_fps = _resolve_numeric_fps(settings, media_src) if want_mp4_video else 30.0
        matte_fps = render_fps if (want_mp4_video and masks) else 30.0
        scale_w, scale_h = (0, 0)
        if want_mp4_video:
            try:
                scale_w, scale_h = (int(p) for p in scale.split(":", 1))
            except (ValueError, AttributeError):
                scale_w, scale_h = (1920, 1080)

        segments = _load_transcription_segments(db, video_id)
        subtitle_entries: list[tuple[float, float, str]] = []
        subtitle_worded: list[dict[str, Any]] = []
        subtitle_layer_entries: list[tuple[float, float, str]] = []
        caption_renderer = "srt"
        caption_style = (
            payload.get("captionStyle")
            if isinstance(payload.get("captionStyle"), dict)
            else {}
        )
        if include_captions:
            # `below_text_chunks` and `above_text_chunks` are only a stacking
            # order; a clip is saying what it is saying on either side of the
            # text layer, so captions come from all of them.
            subtitle_layer_entries = _remap_layer_segments_to_export_timeline(
                db, [*below_text_chunks, *above_text_chunks]
            )
            subtitle_entries = _merge_subtitle_entries(
                _remap_segments_to_export_timeline(segments, normalized, range_rates),
                subtitle_layer_entries,
            )
            if caption_style:
                subtitle_worded = _remap_segments_with_words(
                    segments, normalized, range_rates
                )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            wav_parts: list[Path] = []
            vid_parts: list[Path] = []
            total = len(normalized)

            for index, (start, end) in enumerate(normalized):
                dur = max(0.04, end - start)
                processed_source = _match_audio_range(processed_ranges, start, end)
                w_out = tmp_path / f"w{index:03d}.wav"
                enhanced_audio = _match_audio_range(enhanced_audio_ranges, start, end)
                enhanced_audio_source = (
                    str(enhanced_audio.get("source"))
                    if isinstance(enhanced_audio, dict) and enhanced_audio.get("source")
                    else None
                )
                # Gain/fades/mutes are baked into each segment here, not after
                # the concat: both concats run `-c copy` and a filtered stream
                # cannot be copied. PCM re-encodes anyway, so this is free.
                segment_audio = _transition_audio_settings(
                    # Tolerant: `start`/`end` here have been clamped against the
                    # probed duration, so the last clip's key no longer matches
                    # the browser's numbers exactly.
                    _match_audio_range(audio_ranges, start, end),
                    index,
                    transitions,
                    # The merge step acrossfades these boundaries for real;
                    # folding the halves here too would dip inside the cross.
                    fold_between=not audio_crossfades,
                )
                segment_rate = range_rates[index] if index < len(range_rates) else 1.0
                segment_af = _segment_audio_filter(
                    segment_audio,
                    muted_ranges,
                    segment_start=start,
                    segment_end=start + dur,
                    rate=segment_rate,
                )
                _run_ffmpeg(
                    [
                        "ffmpeg",
                        "-y",
                        "-ss",
                        "0" if enhanced_audio_source else str(start),
                        "-i",
                        enhanced_audio_source or media_src,
                        "-t",
                        str(dur),
                        "-vn",
                        *(["-af", segment_af] if segment_af else []),
                        "-acodec",
                        "pcm_s16le",
                        "-ar",
                        "48000",
                        "-ac",
                        "2",
                        str(w_out),
                    ]
                )
                wav_parts.append(w_out)

                if want_mp4_video:
                    v_out = tmp_path / f"v{index:03d}.mp4"
                    adjust_filters = build_keyframed_adjust_filter_chain(
                        _match_audio_range(color_ranges, start, end),
                        dur,
                    )
                    video_settings = _match_audio_range(video_ranges, start, end)
                    canvas_color = _canvas_background_color(video_settings)
                    use_clip_compositor = _needs_clip_compositor(video_settings)
                    vf_parts = [
                        f"scale={scale}:force_original_aspect_ratio=decrease",
                        f"pad={scale}:(ow-iw)/2:(oh-ih)/2:color={'black@0' if use_clip_compositor else canvas_color}",
                        *adjust_filters,
                        *([] if use_clip_compositor else _motion_blur_filter_parts(video_settings)),
                        "format=rgba",
                    ]
                    # Retiming runs AFTER the source-clock stages (keyframed
                    # adjust windows, mutes) and BEFORE the CFR resample, so
                    # their `t` still means source seconds. The veto in
                    # `_speed_rates_for_ranges` guarantees this is the plain
                    # `-vf` path whenever the rate is not 1.
                    speed_part = (
                        f",setpts=PTS/{segment_rate:.5f}"
                        if abs(segment_rate - 1.0) > 0.001
                        else ""
                    )
                    vf = ",".join(vf_parts) + speed_part + fps_extra

                    matte_path: Path | None = None
                    # Only THIS clip's masks. One flat list used to be applied
                    # to every segment, so a mask drawn on clip 2 also blotted
                    # clips 1 and 3 (plan §5.3 #7).
                    segment_masks = _masks_for_segment(masks, start, end)
                    if segment_masks:
                        try:
                            matte_path = render_matte_video(
                                segment_masks,
                                duration=dur,
                                fps=matte_fps,
                                size=(scale_w, scale_h),
                                out_path=tmp_path / f"matte{index:03d}.mkv",
                                font_warnings=mask_font_fallbacks,
                            )
                        except Exception:
                            if bool(payload.get("masksRequired")):
                                # Fail closed: the caller declared these masks
                                # load-bearing (a redaction, or a harness
                                # composite whose unmasked form is wrong), so
                                # shipping an unmasked export is worse than
                                # shipping none (plan §3.1).
                                raise RuntimeError(
                                    "A required mask failed to render; the export "
                                    "was stopped rather than shipped unmasked."
                                )
                            # A masking bug must never fail a decorative export —
                            # fall back to the unmasked segment. Record the
                            # failure (I11) so the UI can warn the user this
                            # export is unmasked -- masks are sometimes a
                            # redaction, not decoration.
                            logger.exception(
                                "rough_cut_export_job: matte render failed for range %s, exporting without mask",
                                index,
                            )
                            matte_path = None
                            mask_render_failed_for_segment = True

                    _run_ffmpeg(
                        _video_segment_command(
                            video_source=processed_source or media_src,
                            audio_source=enhanced_audio_source or media_src,
                            source_start=start,
                            duration=dur,
                            vf=vf,
                            scale_w=int(scale.split(":", 1)[0]),
                            scale_h=int(scale.split(":", 1)[1]),
                            crf=crf,
                            output=v_out,
                            processed=processed_source is not None,
                            matte_path=matte_path,
                            background_color=canvas_color,
                            clip_settings=video_settings,
                            frame_rate=render_fps,
                            # The identical chain the WAV path bakes in -- the
                            # two formats must sound the same for one edit.
                            audio_filter=segment_af,
                            audio_processed=enhanced_audio_source is not None,
                        )
                    )
                    vid_parts.append(v_out)

                progress = min(72, int(8 + ((index + 1) / max(total, 1)) * 64))
                row.result_data = _merge_result_payload(row.result_data, {"progress": progress})
                db.commit()

            merged_wav = tmp_path / "merged.wav"
            crossfade_command = _crossfade_wav_command(
                wav_parts, audio_crossfades, merged_wav
            )
            if crossfade_command:
                _run_ffmpeg(crossfade_command)
            else:
                wav_list = tmp_path / "list_wav.txt"
                wav_list.write_text("".join(f"file '{p.as_posix()}'\n" for p in wav_parts), encoding="utf-8")
                _run_ffmpeg(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(wav_list), "-c", "copy", str(merged_wav)])

            # Without a video pass there is no compositor to fold audio-lane
            # clips into, so they are mixed straight onto the merged A-roll --
            # otherwise a WAV (or an audio-only MP4) exports without the music
            # the editor was playing.
            if audio_lane_chunks and not want_mp4_video:
                mix_command = _timeline_audio_mix_command(
                    base_audio=merged_wav,
                    chunks=audio_lane_chunks,
                    output=tmp_path / "merged_mixed.wav",
                    duck=duck_music,
                )
                if mix_command:
                    _run_ffmpeg(mix_command)
                    merged_wav = tmp_path / "merged_mixed.wav"

            if fmt == "wav":
                if loudness_target in _LOUDNESS_TARGETS:
                    merged_wav, loudness_warnings = _apply_loudness_target(
                        merged_wav, tmp_path, loudness_target,
                        has_video=False, suffix="out.wav",
                    )
                    caption_warnings.extend(loudness_warnings)
                final_path = merged_wav
                public_hint = uuid.uuid4().hex[:12]
                url = upload_local_path_to_cloudinary(
                    str(final_path),
                    resource_type="raw",
                    folder="rough-cut-exports",
                    public_id=f"w_{video_id}_{public_hint}",
                )
                # WAV is an audio-only download, not a playable video — never
                # register it as a version even if register_as_version was set.
                row.result_data = _merge_result_payload(
                    row.result_data,
                    {
                        "progress": 100,
                        "downloadUrl": url,
                        "format": fmt,
                        "burnInSkipped": burn_skipped,
                        "captionEntries": len(subtitle_entries),
                        "loudnessTarget": loudness_target
                        if loudness_target in _LOUDNESS_TARGETS
                        else None,
                        **({"warnings": caption_warnings} if caption_warnings else {}),
                    },
                )
                row.status = "completed"
                db.commit()
                logger.info("rough_cut_export_job: WAV completed video_id=%s", video_id)
                return

            if want_mp4_video and vid_parts:
                merged_vid = tmp_path / "merged_v.mp4"
                transition_command = _transition_video_command(
                    vid_parts,
                    [
                        max(0.04, _output_span(start, end, range_rates[index]))
                        for index, (start, end) in enumerate(normalized)
                    ],
                    transitions,
                    audio_path=merged_wav,
                    crf=crf,
                    output=merged_vid,
                )
                if transition_command:
                    _run_ffmpeg(transition_command)
                else:
                    v_list = tmp_path / "list_v.txt"
                    v_list.write_text("".join(f"file '{p.as_posix()}'\n" for p in vid_parts), encoding="utf-8")
                    _run_ffmpeg(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(v_list), "-c", "copy", str(merged_vid)])
                final_video = merged_vid

                if timeline_tail > 0.02:
                    extended = tmp_path / "merged_v_tail.mp4"
                    _run_ffmpeg(
                        _extend_base_timeline_command(
                            base_video=final_video,
                            tail_duration=timeline_tail,
                            has_audio=_ffprobe_has_audio(str(final_video)),
                            frame_rate=render_fps,
                            crf=crf,
                            output=extended,
                        )
                    )
                    final_video = extended

                # Audio lanes ride along with the first compositing pass: it
                # already opens every layer as an input and mixes them, and
                # `_timeline_layers_command` keeps them out of the picture
                # graph. When there is no picture layer at all the pass runs
                # for the mix alone and stream-copies the video.
                below_pass_chunks = [*below_text_chunks, *audio_lane_chunks]
                if below_pass_chunks:
                    below_text_video = tmp_path / "merged_below_text.mp4"
                    _run_ffmpeg(
                        _timeline_layers_command(
                            base_video=final_video,
                            chunks=below_pass_chunks,
                            scale_w=scale_w,
                            scale_h=scale_h,
                            frame_rate=render_fps,
                            crf=crf,
                            output=below_text_video,
                            base_has_audio=_ffprobe_has_audio(str(final_video)),
                            duck=duck_music,
                        )
                    )
                    final_video = below_text_video

                if include_captions and subtitle_entries:
                    caption_file, caption_renderer, caption_warnings = (
                        _build_caption_burn_file(
                            tmp_path,
                            plain_entries=subtitle_entries,
                            worded_entries=subtitle_worded,
                            layer_entries=subtitle_layer_entries,
                            caption_style=caption_style,
                            scale_w=scale_w,
                            scale_h=scale_h,
                        )
                    )
                    sub_path = _escape_subtitles_filter_path(caption_file)
                    fonts_dir = (os.environ.get("CAPTION_FONTS_DIR") or "").strip()
                    if fonts_dir and caption_renderer == "ass":
                        sub_path += (
                            ":fontsdir="
                            + _escape_subtitles_filter_path(Path(fonts_dir))
                        )
                    burned = tmp_path / "merged_burned.mp4"
                    _run_ffmpeg(
                        [
                            "ffmpeg",
                            "-y",
                            "-i",
                            str(final_video),
                            "-vf",
                            f"subtitles={sub_path}",
                            "-c:v",
                            "libx264",
                            "-preset",
                            "veryfast",
                            "-crf",
                            str(crf),
                            "-pix_fmt",
                            "yuv420p",
                            "-c:a",
                            "copy",
                            "-movflags",
                            "+faststart",
                            str(burned),
                        ]
                    )
                    final_video = burned

                # Caption/text tracks are burned onto the base first. Picture
                # clips on the upper V tracks are then composited over them,
                # producing the common V2 / TX1 / V1 cutout-title sandwich.
                if above_text_chunks:
                    layered = tmp_path / "merged_layered.mp4"
                    _run_ffmpeg(
                        _timeline_layers_command(
                            base_video=final_video,
                            chunks=above_text_chunks,
                            scale_w=scale_w,
                            scale_h=scale_h,
                            frame_rate=render_fps,
                            crf=crf,
                            output=layered,
                            base_has_audio=_ffprobe_has_audio(str(final_video)),
                        )
                    )
                    final_video = layered

                if shorts_export:
                    tw, th = 1080, 1920
                    vf_short = f"scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th}"
                    shorts_out = tmp_path / "merged_shorts.mp4"
                    _run_ffmpeg(
                        [
                            "ffmpeg",
                            "-y",
                            "-i",
                            str(final_video),
                            "-vf",
                            vf_short,
                            "-c:v",
                            "libx264",
                            "-preset",
                            "veryfast",
                            "-crf",
                            str(crf),
                            "-pix_fmt",
                            "yuv420p",
                            "-c:a",
                            "aac",
                            "-b:a",
                            "192k",
                            "-movflags",
                            "+faststart",
                            str(shorts_out),
                        ]
                    )
                    final_video = shorts_out

                # Rasterized overlays (text, lower thirds, brand) go on last of
                # all: after the captions and the upper picture tracks, because
                # a lower third belongs over the picture rather than under it,
                # and after the 9:16 crop because the client rasterizes for the
                # frame that is actually delivered. Compositing before the crop
                # squashed a vertical PNG into the landscape frame and then cut
                # a third of it away.
                if raw_burn_ins:
                    rendered_duration = _ffprobe_duration(str(final_video))
                    if rendered_duration is None or rendered_duration <= 0:
                        rendered_duration = kept_total + timeline_tail
                    burn_in_entries, burn_in_reasons = _sanitize_burn_ins(
                        raw_burn_ins,
                        tmp_path=tmp_path,
                        output_duration=float(rendered_duration),
                    )
                    burn_skipped.extend(burn_in_reasons)

                # No valid frames means no pass at all -- an export without
                # burn-ins comes out of exactly the ffmpeg chain it always did.
                if burn_in_entries:
                    probed_size = _ffprobe_video_size(str(final_video))
                    burn_w, burn_h = probed_size or (scale_w, scale_h)
                    burned_overlays = tmp_path / "merged_burn_ins.mp4"
                    _run_ffmpeg(
                        _burn_in_overlay_command(
                            base_video=final_video,
                            burn_ins=burn_in_entries,
                            width=burn_w,
                            height=burn_h,
                            frame_rate=render_fps,
                            crf=crf,
                            output=burned_overlays,
                        )
                    )
                    final_video = burned_overlays

                if loudness_target in _LOUDNESS_TARGETS and _ffprobe_has_audio(
                    str(final_video)
                ):
                    final_video, loudness_warnings = _apply_loudness_target(
                        final_video, tmp_path, loudness_target,
                        has_video=True, suffix="out.mp4",
                    )
                    caption_warnings.extend(loudness_warnings)
                final_path = final_video
                resource_type = "video"
                url = upload_local_path_to_cloudinary(
                    str(final_path),
                    resource_type="video",
                    folder="rough-cut-exports",
                    public_id=f"mp4_{video_id}_{uuid.uuid4().hex[:10]}",
                )
            else:
                mp4_out = tmp_path / "merged_a.mp4"
                _run_ffmpeg(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(merged_wav),
                        "-c:a",
                        "aac",
                        "-b:a",
                        "192k",
                        "-vn",
                        "-movflags",
                        "+faststart",
                        str(mp4_out),
                    ]
                )
                if loudness_target in _LOUDNESS_TARGETS:
                    mp4_out, loudness_warnings = _apply_loudness_target(
                        mp4_out, tmp_path, loudness_target,
                        has_video=False, suffix="audio.m4a",
                    )
                    caption_warnings.extend(loudness_warnings)
                url = upload_local_path_to_cloudinary(
                    str(mp4_out),
                    resource_type="video",
                    folder="rough-cut-exports",
                    public_id=f"mp4audio_{video_id}_{uuid.uuid4().hex[:10]}",
                )
                final_path = mp4_out

            meta = {
                "progress": 100,
                "downloadUrl": url,
                "format": "mp4",
                "burnInSkipped": burn_skipped,
                "captionEntries": len(subtitle_entries),
                "captionRenderer": caption_renderer if subtitle_entries else None,
                "timelineLayerChunks": len(timeline_chunks),
                "shortsExport": shorts_export,
                "burnInsApplied": len(burn_in_entries),
                "burnInsAnimated": sum(1 for entry in burn_in_entries if entry.get("motion")),
                "loudnessTarget": loudness_target if loudness_target in _LOUDNESS_TARGETS else None,
            }
            if caption_warnings:
                meta["warnings"] = [*meta.get("warnings", []), *caption_warnings]
            if masks and mask_render_failed_for_segment:
                # I11: masks were requested but at least one segment's matte
                # failed to render, so this export shipped unmasked -- the UI
                # must be able to warn the user (a mask can be a redaction).
                meta["maskingFailed"] = True
            if mask_font_fallbacks:
                meta["warnings"] = [
                    *meta.get("warnings", []),
                    "Mask text font "
                    + ", ".join(sorted(mask_font_fallbacks))
                    + " is not available on the export worker; the default mask font was used instead.",
                ]
                meta["maskFontFallbacks"] = sorted(mask_font_fallbacks)

            if register_as_version and video is not None:
                try:
                    from app.services.video_versions import register_video_version

                    try:
                        export_size_bytes = final_path.stat().st_size
                    except OSError:
                        export_size_bytes = None
                    new_video = register_video_version(
                        db,
                        video,
                        name=f"{video.name} (edited)",
                        file_path=url,
                        size_bytes=export_size_bytes,
                    )
                    db.commit()
                    meta["versionVideoId"] = new_video.id
                except Exception:  # noqa: BLE001
                    # Registration must never fail the export. `meta` is still
                    # just a local dict at this point (nothing committed yet) —
                    # db.rollback() only undoes the failed registration's
                    # uncommitted Video insert. The downloadUrl write into
                    # row.result_data and its commit happen unconditionally
                    # below, regardless of this failure.
                    db.rollback()
                    logger.exception(
                        "rough_cut_export_job: failed to register export as version for video_id=%s",
                        video_id,
                    )

            row.result_data = _merge_result_payload(row.result_data, meta)
            row.status = "completed"
            used_features: set[str] = set()
            if subtitle_entries:
                used_features.add("captions")
            if burn_in_entries:
                used_features.add("text_overlay")
            if masks and not meta.get("maskingFailed"):
                used_features.add("masking")
            if transitions:
                used_features.add("transitions")
            _record_export_feature_result_use(
                db,
                row=row,
                video=video,
                feature_keys=used_features,
            )
            db.commit()
            logger.info("rough_cut_export_job: MP4 completed video_id=%s", video_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("rough_cut_export_job failed ai_result_id=%s", ai_result_id)
        try:
            row = db.query(AiResult).filter(AiResult.id == ai_result_id).first()
            if row:
                msg = str(exc)[:4000]
                row.status = "failed"
                row.error_message = msg
                row.result_data = _merge_result_payload(
                    getattr(row, "result_data", None), {"progress": 0, "error": msg}
                )
                db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("rough_cut_export_job: failed to persist error state")
        raise

    finally:
        db.close()
