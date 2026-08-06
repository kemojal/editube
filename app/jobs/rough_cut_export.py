"""
RQ job: concatenate rough-cut keepRanges via ffmpeg and upload to Cloudinary.

Supports: frameRate from exportSettings, optional burned-in subtitles from DB
transcription + keepRanges, optional 9:16 shorts crop, and metadata when
lower-thirds / brand burn-in is requested but not yet implemented.
"""

from __future__ import annotations

import logging
import math
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.db.models import AiResult, Video, VideoTranscription
from app.services.mask_matte import render_matte_video
from app.services.color_adjust_keyframes import build_keyframed_adjust_filter_chain
from app.jobs.rough_cut_effect import _resolve_media_source
from app.utils.cloudinary import cloudinary_credentials_configured, upload_local_path_to_cloudinary

logger = logging.getLogger(__name__)


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
    out.sort(key=lambda x: x[0])
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


def _eased_ratio(ratio: str, easing: str) -> str:
    if easing == "hold":
        return "0"
    if easing == "ease-in":
        return f"({ratio})*({ratio})"
    if easing == "ease-out":
        return f"1-(1-({ratio}))*(1-({ratio}))"
    if easing == "ease-in-out":
        return f"if(lt(({ratio}),0.5),2*({ratio})*({ratio}),1-pow(-2*({ratio})+2,2)/2)"
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
        result["scale"] = f"1+({phase})*{0.12 * intensity:.6f}"
    elif combo == "spin":
        result["rotation"] = f"({phase})*{12 * intensity:.6f}"
    elif combo in {"slide-left", "slide-right"}:
        result["x"] = f"({phase})*{(-8 if combo == 'slide-left' else 8) * intensity:.6f}"
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
    numeric_defaults = {"scale": 100, "scaleY": 100, "x": 0, "y": 0, "rotation": 0, "opacity": 100, "cornerRadius": 0}
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
    presets = _animation_presets(settings)
    alpha_active = (
        abs(_number_between(video.get("opacity"), 0, 100, 100) - 100) > 0.0001
        or abs(_number_between(video.get("cornerRadius"), 0, 50, 0)) > 0.0001
        or bool(_keyframe_track(settings, "video.opacity", duration))
        or bool(_keyframe_track(settings, "video.cornerRadius", duration))
        or presets[0] != "none"
        or presets[1] != "none"
        or presets[2] == "fade"
    )
    alpha_clip = cutout
    if alpha_active:
        graph.append(
            f"{cutout}geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
            f"a='alpha(X,Y)*({opacity_expression})*({corner_alpha})'[alpha_clip]"
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

    scale_x_expression = f"min(6,max(0.01,(({scale_x})/100)*({animation['scale']})))"
    scale_y_expression = f"min(6,max(0.01,(({scale_y})/100)*({animation['scale']})))"
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
    overlay_x = f"(W-w)/2+((({x})+({animation['x']}))/100)*W"
    overlay_y = f"(H-h)/2+((({y})+({animation['y']}))/100)*H"
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
) -> list[str]:
    """Build one MP4 segment while preserving a processed source's alpha."""
    command = ["ffmpeg", "-y"]
    if processed:
        command += ["-i", video_source, "-ss", str(source_start), "-i", audio_source]
        audio_input = 1
    else:
        command += ["-ss", str(source_start), "-i", video_source]
        audio_input = 0

    matte_input: int | None = None
    if matte_path is not None:
        matte_input = 2 if processed else 1
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
) -> list[tuple[float, float, str]]:
    """Intersect segment timings with kept ranges; map to concatenated export time."""
    out: list[tuple[float, float, str]] = []
    if not normalized_ranges:
        return out
    t_off = 0.0
    for ks, ke in normalized_ranges:
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
            out_s = t_off + (a - ks)
            out_e = t_off + (b - ks)
            out.append((out_s, out_e, text))
        t_off += ke - ks
    out.sort(key=lambda x: x[0])
    return out


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
            logger.error("rough_cut_export_job: missing row id=%s", ai_result_id)
            return

        payload = dict(row.result_data or {})
        video_id = row.video_id
        fmt = str(payload.get("format") or "mp4").lower()

        video = db.query(Video).filter(Video.id == video_id).first()
        media_src = (video.file_path if video else "") or ""
        # I8: clamp keep-ranges against the real source duration before
        # anything downstream (matte rendering, ffmpeg -t) can be pinned by
        # a hostile/malformed range extending far past the actual media.
        source_duration = _ffprobe_duration(media_src) if media_src else None
        normalized = _normalize_ranges(payload.get("keepRanges"), max_end=source_duration)

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
        include_lt = bool(settings.get("includeLowerThirds", False))
        include_brand = bool(settings.get("includeBrand", False))
        shorts_export = bool(settings.get("shortsExport") or settings.get("verticalExport"))

        burn_skipped: list[str] = []
        if include_lt:
            burn_skipped.append("lowerThirds")
        if include_brand:
            burn_skipped.append("brand")

        has_video = _ffprobe_has_video(media_src)
        want_mp4_video = fmt == "mp4" and has_video

        if not cloudinary_credentials_configured():
            raise RuntimeError("Cloudinary credentials are required to finish exports (CLOUDINARY_* env).")

        fps_extra = _fps_filter_part(settings, media_src) if want_mp4_video else ""
        masks = payload.get("masks") if isinstance(payload.get("masks"), list) else []
        processed_ranges = _approved_processed_ranges(
            db, video_id, payload.get("processedRanges")
        )
        color_ranges = _range_settings(payload.get("colorRanges"))
        video_ranges = _range_settings(payload.get("videoRanges"))
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
        if want_mp4_video and masks:
            try:
                scale_w, scale_h = (int(p) for p in scale.split(":", 1))
            except (ValueError, AttributeError):
                scale_w, scale_h = (1920, 1080)

        segments = _load_transcription_segments(db, video_id)
        subtitle_entries = _remap_segments_to_export_timeline(segments, normalized) if include_captions else []

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            wav_parts: list[Path] = []
            vid_parts: list[Path] = []
            total = len(normalized)

            for index, (start, end) in enumerate(normalized):
                dur = max(0.04, end - start)
                processed_source = processed_ranges.get(
                    (round(start, 3), round(end, 3))
                )
                w_out = tmp_path / f"w{index:03d}.wav"
                _run_ffmpeg(
                    [
                        "ffmpeg",
                        "-y",
                        "-ss",
                        str(start),
                        "-i",
                        media_src,
                        "-t",
                        str(dur),
                        "-vn",
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
                        color_ranges.get((round(start, 3), round(end, 3))),
                        dur,
                    )
                    video_settings = video_ranges.get((round(start, 3), round(end, 3)))
                    canvas_color = _canvas_background_color(video_settings)
                    use_clip_compositor = _needs_clip_compositor(video_settings)
                    vf_parts = [
                        f"scale={scale}:force_original_aspect_ratio=decrease",
                        f"pad={scale}:(ow-iw)/2:(oh-ih)/2:color={'black@0' if use_clip_compositor else canvas_color}",
                        *adjust_filters,
                        *([] if use_clip_compositor else _motion_blur_filter_parts(video_settings)),
                        "format=rgba",
                    ]
                    vf = ",".join(vf_parts) + fps_extra

                    matte_path: Path | None = None
                    if masks:
                        try:
                            matte_path = render_matte_video(
                                masks,
                                duration=dur,
                                fps=matte_fps,
                                size=(scale_w, scale_h),
                                out_path=tmp_path / f"matte{index:03d}.mkv",
                                font_warnings=mask_font_fallbacks,
                            )
                        except Exception:
                            # A masking bug must never fail the whole export —
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
                            audio_source=media_src,
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
                        )
                    )
                    vid_parts.append(v_out)

                progress = min(72, int(8 + ((index + 1) / max(total, 1)) * 64))
                row.result_data = _merge_result_payload(row.result_data, {"progress": progress})
                db.commit()

            wav_list = tmp_path / "list_wav.txt"
            wav_list.write_text("".join(f"file '{p.as_posix()}'\n" for p in wav_parts), encoding="utf-8")
            merged_wav = tmp_path / "merged.wav"
            _run_ffmpeg(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(wav_list), "-c", "copy", str(merged_wav)])

            if fmt == "wav":
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
                    },
                )
                row.status = "completed"
                db.commit()
                logger.info("rough_cut_export_job: WAV completed video_id=%s", video_id)
                return

            if want_mp4_video and vid_parts:
                v_list = tmp_path / "list_v.txt"
                v_list.write_text("".join(f"file '{p.as_posix()}'\n" for p in vid_parts), encoding="utf-8")
                merged_vid = tmp_path / "merged_v.mp4"
                _run_ffmpeg(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(v_list), "-c", "copy", str(merged_vid)])
                final_video = merged_vid

                if include_captions and subtitle_entries:
                    srt_path = tmp_path / "burnin.srt"
                    _write_srt(srt_path, subtitle_entries)
                    sub_path = _escape_subtitles_filter_path(srt_path)
                    burned = tmp_path / "merged_burned.mp4"
                    _run_ffmpeg(
                        [
                            "ffmpeg",
                            "-y",
                            "-i",
                            str(merged_vid),
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
                "shortsExport": shorts_export,
            }
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

    finally:
        db.close()
