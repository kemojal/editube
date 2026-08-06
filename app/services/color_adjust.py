"""Deterministic color-adjust filters shared by effect renders and final export."""

from __future__ import annotations

import math
import subprocess
from typing import Any


def _number(value: Any, fallback: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if math.isfinite(number) else fallback


def _clamp(value: Any, low: float, high: float, fallback: float = 0.0) -> float:
    return max(low, min(high, _number(value, fallback)))


def _curve_points(value: Any) -> str | None:
    if not isinstance(value, list):
        return None
    points: list[tuple[float, float]] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        points.append((_clamp(raw.get("x"), 0, 1), _clamp(raw.get("y"), 0, 1)))
    if len(points) < 2:
        return None
    points.sort(key=lambda point: point[0])
    unique: list[tuple[float, float]] = []
    for point in points:
        if unique and abs(point[0] - unique[-1][0]) < 0.001:
            unique[-1] = point
        else:
            unique.append(point)
    if len(unique) < 2:
        return None
    return " ".join(f"{x:.4f}/{y:.4f}" for x, y in unique)


def _tone_curve(settings: dict[str, Any]) -> str | None:
    blacks = _clamp(settings.get("blacks"), -100, 100) / 100
    shadows = _clamp(settings.get("shadow"), -100, 100) / 100
    brilliance = _clamp(settings.get("brilliance"), -100, 100) / 100
    highlights = _clamp(settings.get("highlight"), -100, 100) / 100
    whites = _clamp(settings.get("whites"), -100, 100) / 100
    wheels = settings.get("wheels") if isinstance(settings.get("wheels"), dict) else {}
    shadow_wheel = wheels.get("shadows") if isinstance(wheels.get("shadows"), dict) else {}
    mid_wheel = wheels.get("midtones") if isinstance(wheels.get("midtones"), dict) else {}
    highlight_wheel = wheels.get("highlights") if isinstance(wheels.get("highlights"), dict) else {}
    shadows += _clamp(shadow_wheel.get("luminance"), -100, 100) / 130
    brilliance += _clamp(mid_wheel.get("luminance"), -100, 100) / 150
    highlights += _clamp(highlight_wheel.get("luminance"), -100, 100) / 130

    if not any(abs(value) > 0.0001 for value in (blacks, shadows, brilliance, highlights, whites)):
        return None

    ys = [
        max(0.0, blacks * 0.10),
        0.25 + shadows * 0.16 + brilliance * 0.03 - max(0.0, -blacks) * 0.05,
        0.50 + brilliance * 0.13 + shadows * 0.035 + highlights * 0.035,
        0.75 + highlights * 0.16 + brilliance * 0.03 + max(0.0, whites) * 0.04,
        min(1.0, 1.0 + whites * 0.10),
    ]
    # Curves must stay monotonic or highlights can fold back into shadows.
    ys = [max(0.0, min(1.0, value)) for value in ys]
    for index in range(1, len(ys)):
        ys[index] = max(ys[index], min(1.0, ys[index - 1] + 0.01))
    for index in range(len(ys) - 2, -1, -1):
        ys[index] = min(ys[index], max(0.0, ys[index + 1] - 0.01))
    return " ".join(
        f"{x:.4f}/{y:.4f}" for x, y in zip((0.0, 0.25, 0.5, 0.75, 1.0), ys)
    )


def _wheel_vector(wheel: Any) -> tuple[float, float, float]:
    if not isinstance(wheel, dict):
        return (0.0, 0.0, 0.0)
    saturation = _clamp(wheel.get("saturation"), 0, 100) / 100
    if saturation <= 0:
        return (0.0, 0.0, 0.0)
    hue = math.radians(_clamp(wheel.get("hue"), 0, 360))
    # Three phase-shifted cosines form a smooth RGB wheel. Removing their mean
    # keeps a color push from changing luminance before the wheel's own luma
    # slider is applied by `_tone_curve`.
    raw = [math.cos(hue), math.cos(hue - 2 * math.pi / 3), math.cos(hue + 2 * math.pi / 3)]
    mean = sum(raw) / 3
    return tuple((value - mean) * saturation * 0.32 for value in raw)


def build_adjust_filter_chain(settings: dict[str, Any] | None) -> list[str]:
    """Return a safe ffmpeg filter list for all inspector adjustment controls."""
    if not isinstance(settings, dict) or settings.get("enabled") is False:
        return []

    filters: list[str] = []
    temperature = _clamp(settings.get("temp"), -100, 100)
    if abs(temperature) > 0.001:
        kelvin = max(2500.0, min(11000.0, 6500.0 + temperature * 45.0))
        filters.append(f"colortemperature=temperature={kelvin:.1f}:mix={min(1.0, abs(temperature) / 70):.4f}:pl=0.65")

    tint = _clamp(settings.get("tint"), -100, 100) / 100
    wheels = settings.get("wheels") if isinstance(settings.get("wheels"), dict) else {}
    shadow_rgb = _wheel_vector(wheels.get("shadows"))
    mid_rgb = _wheel_vector(wheels.get("midtones"))
    high_rgb = _wheel_vector(wheels.get("highlights"))
    mid_rgb = (mid_rgb[0] + tint * 0.11, mid_rgb[1] - tint * 0.16, mid_rgb[2] + tint * 0.11)
    if any(abs(value) > 0.0001 for value in (*shadow_rgb, *mid_rgb, *high_rgb)):
        values = (*shadow_rgb, *mid_rgb, *high_rgb)
        filters.append(
            "colorbalance="
            + ":".join(
                f"{name}={max(-1.0, min(1.0, value)):.4f}"
                for name, value in zip(("rs", "gs", "bs", "rm", "gm", "bm", "rh", "gh", "bh"), values)
            )
            + ":pl=1"
        )

    exposure = _clamp(settings.get("exposure"), -100, 100) * 0.022
    black = max(-0.25, min(0.25, -_clamp(settings.get("blacks"), -100, 100) / 550))
    if abs(exposure) > 0.0001 or abs(black) > 0.0001:
        filters.append(f"exposure=exposure={exposure:.4f}:black={black:.4f}")

    contrast = max(0.05, 1 + _clamp(settings.get("contrast"), -100, 100) / 125)
    saturation = max(0.0, 1 + _clamp(settings.get("saturation"), -100, 100) / 100)
    if abs(contrast - 1) > 0.0001 or abs(saturation - 1) > 0.0001:
        filters.append(f"eq=contrast={contrast:.4f}:saturation={saturation:.4f}")

    vibrance = _clamp(settings.get("vibrance"), -100, 100) / 125
    if abs(vibrance) > 0.0001:
        filters.append(f"vibrance=intensity={vibrance:.4f}")

    tone = _tone_curve(settings)
    if tone:
        filters.append(f"curves=master='{tone}'")

    hsl = settings.get("hsl") if isinstance(settings.get("hsl"), dict) else {}
    color_flags = {
        "red": "r",
        "orange": "r+y",
        "yellow": "y",
        "green": "g",
        "cyan": "c",
        "blue": "b",
        "violet": "b+m",
        "magenta": "m",
    }
    for channel, flags in color_flags.items():
        values = hsl.get(channel) if isinstance(hsl.get(channel), dict) else {}
        hue = _clamp(values.get("hue"), -100, 100) * 0.30
        sat = _clamp(values.get("saturation"), -100, 100) / 100
        intensity = _clamp(values.get("brightness"), -100, 100) / 220
        if any(abs(value) > 0.0001 for value in (hue, sat, intensity)):
            strength = 55 if "+" in flags else 100
            filters.append(
                f"huesaturation=colors={flags}:hue={hue:.4f}:saturation={sat:.4f}:"
                f"intensity={intensity:.4f}:strength={strength}:lightness=1"
            )

    curves = settings.get("curves") if isinstance(settings.get("curves"), dict) else {}
    curve_options: list[str] = []
    for key, ffmpeg_key in (("master", "master"), ("red", "red"), ("green", "green"), ("blue", "blue")):
        points = _curve_points(curves.get(key))
        if points:
            curve_options.append(f"{ffmpeg_key}='{points}'")
    if curve_options:
        filters.append("curves=" + ":".join(curve_options) + ":interp=pchip")

    fade = _clamp(settings.get("fade"), 0, 100) / 100
    if fade > 0:
        lift = fade * 0.12
        filters.append(
            "curves=master='"
            f"0.0000/{lift:.4f} 0.5000/{0.5 + lift * 0.18:.4f} 1.0000/{1.0 - lift * 0.45:.4f}'"
        )
    sharpen = _clamp(settings.get("sharpen"), 0, 100) / 100
    if sharpen > 0:
        filters.append(f"unsharp=5:5:{sharpen * 1.35:.4f}:5:5:0")
    vignette = _clamp(settings.get("vignette"), 0, 100) / 100
    if vignette > 0:
        filters.append(f"vignette=angle=PI/{max(2.5, 8.0 - vignette * 4.5):.4f}:eval=frame")
    grain = _clamp(settings.get("grain"), 0, 100) / 100
    if grain > 0:
        filters.append(f"noise=alls={grain * 24:.3f}:allf=t+u")
    return filters


def build_adjust_filter(settings: dict[str, Any] | None) -> str:
    return ",".join(build_adjust_filter_chain(settings))


def apply_adjust_frame(frame_bgr: Any, settings: dict[str, Any] | None):
    """Apply the exact export filter chain to one BGR preview frame.

    This deliberately invokes ffmpeg rather than maintaining a second partial
    color engine in OpenCV. Paused preview and export therefore share the same
    temperature, HSL, curve, wheel, vignette, and grain behavior.
    """
    chain = build_adjust_filter(settings)
    if not chain:
        return frame_bgr.copy()
    if getattr(frame_bgr, "ndim", 0) != 3 or frame_bgr.shape[2] != 3:
        raise ValueError("Adjustment preview needs a three-channel video frame.")

    height, width = frame_bgr.shape[:2]
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-s",
        f"{width}x{height}",
        "-i",
        "pipe:0",
        "-vf",
        chain,
        "-frames:v",
        "1",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "pipe:1",
    ]
    result = subprocess.run(
        command,
        input=frame_bgr.tobytes(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=20,
    )
    if result.returncode != 0:
        detail = (result.stderr.decode("utf-8", "replace").strip().splitlines() or ["unknown filter error"])[-1]
        raise RuntimeError(f"Could not render adjustment preview: {detail}")
    expected = width * height * 3
    if len(result.stdout) != expected:
        raise RuntimeError("Adjustment preview returned an incomplete frame.")

    import numpy as np  # type: ignore

    return np.frombuffer(result.stdout, dtype=np.uint8).reshape((height, width, 3)).copy()
