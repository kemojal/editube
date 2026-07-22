"""Render one UGC variation to an MP4.

Pipeline: provider avatar render → scale/crop to aspect → composite text
overlays (disclosure label + hook + CTA) → thumbnail → Cloudinary (or local in
dry-run). Text is rendered to transparent PNGs with Pillow and composited via
ffmpeg's core ``overlay`` filter, so it works regardless of whether the local
ffmpeg was built with libass/drawtext.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path
from typing import Callable, Optional

import httpx
from sqlalchemy.orm import Session

from app.db.models import UgcCampaign, UgcVariation, Workspace
from app.services.clip_renderer import ASPECT_RATIOS, _extract_thumbnail, _scale_crop
from app.services.ugc_compliance import DISCLOSURE_LABEL, disclosure_enabled
from app.ugc_providers import get_avatar_provider
from app.utils.cloudinary import (
    cloudinary_credentials_configured,
    upload_local_path_to_cloudinary,
)

logger = logging.getLogger(__name__)

UGC_OUTPUT_DIR = Path(os.environ.get("UGC_OUTPUT_DIR", "./uploads/ugc")).resolve()
UGC_PUBLIC_PREFIX = os.environ.get("UGC_PUBLIC_PREFIX", "/uploads/ugc")

_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

ProgressCb = Callable[[int], None]


def _dry_run() -> bool:
    return os.environ.get("UGC_RENDER_DRY_RUN", "").strip().lower() in ("1", "true", "yes")


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-1200:]
        raise RuntimeError(f"ffmpeg failed ({proc.returncode}): {tail}")


def _probe_duration(path: Path) -> float | None:
    """Return media duration in seconds via ffprobe, or None on failure."""
    ffprobe = os.environ.get("FFPROBE_PATH", "ffprobe")
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return float(proc.stdout.strip())
    except Exception:  # noqa: BLE001
        pass
    return None


def _font(size: int):
    from PIL import ImageFont

    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:  # noqa: BLE001
            continue
    return ImageFont.load_default()


def _text_png(text: str, w: int, h: int, *, anchor_y: str, font_size: int, out_path: Path) -> None:
    """Render ``text`` onto a transparent w×h PNG, wrapped, with an outline."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _font(font_size)
    max_w = int(w * 0.86)
    # Wrap by measuring; estimate chars-per-line then refine.
    avg_char = max(1, int(draw.textlength("M", font=font)))
    approx = max(8, max_w // avg_char)
    lines: list[str] = []
    for para in (text or "").split("\n"):
        lines.extend(textwrap.wrap(para, width=approx) or [""])
    line_h = int(font_size * 1.25)
    block_h = line_h * len(lines)
    if anchor_y == "top":
        y = int(h * 0.04)
    elif anchor_y == "bottom":
        y = int(h * 0.80) - block_h
    else:  # center
        y = (h - block_h) // 2
    stroke = max(2, font_size // 12)
    for line in lines:
        tw = draw.textlength(line, font=font)
        x = (w - tw) // 2
        draw.text(
            (x, y), line, font=font, fill=(255, 255, 255, 255),
            stroke_width=stroke, stroke_fill=(0, 0, 0, 235),
        )
        y += line_h
    img.save(out_path)


def _materialize_source(video_url: str, dst: Path) -> Path:
    if video_url.startswith(("http://", "https://")):
        with httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0), follow_redirects=True) as c:
            with c.stream("GET", video_url) as r:
                r.raise_for_status()
                with open(dst, "wb") as f:
                    for chunk in r.iter_bytes():
                        f.write(chunk)
        return dst
    src = Path(video_url)
    if not src.exists():
        raise FileNotFoundError(f"provider video missing: {video_url}")
    shutil.copyfile(src, dst)
    return dst


def _composite_overlays(
    scaled: Path,
    out: Path,
    *,
    w: int,
    h: int,
    duration: float,
    hook: str,
    cta: str,
    disclosure: bool,
    work: Path,
) -> bool:
    """Composite disclosure/hook/CTA text. Returns True if any overlay applied."""
    specs: list[tuple[Path, Optional[str]]] = []
    if disclosure:
        p = work / "ov_disc.png"
        _text_png(DISCLOSURE_LABEL, w, h, anchor_y="top", font_size=max(22, int(h * 0.022)), out_path=p)
        specs.append((p, None))
    if hook:
        p = work / "ov_hook.png"
        _text_png(hook, w, h, anchor_y="center", font_size=max(40, int(h * 0.05)), out_path=p)
        specs.append((p, f"between(t,0,{min(3.5, duration):.2f})"))
    if cta:
        p = work / "ov_cta.png"
        _text_png(cta, w, h, anchor_y="bottom", font_size=max(34, int(h * 0.04)), out_path=p)
        specs.append((p, f"between(t,{max(0.0, duration - 3.5):.2f},{duration:.2f})"))
    if not specs:
        shutil.copyfile(scaled, out)
        return False

    inputs = ["-i", str(scaled)]
    filters = []
    prev = "[0:v]"
    for i, (png, enable) in enumerate(specs, start=1):
        inputs += ["-i", str(png)]
        label = f"[v{i}]"
        en = f":enable='{enable}'" if enable else ""
        filters.append(f"{prev}[{i}:v]overlay=0:0{en}{label}")
        prev = label
    _run(
        [
            "ffmpeg", "-y", *inputs,
            "-filter_complex", ";".join(filters),
            "-map", prev, "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "copy", "-movflags", "+faststart",
            str(out),
        ]
    )
    return True


def render_variation(db: Session, variation_id: int, *, on_progress: ProgressCb | None = None) -> str:
    var: UgcVariation | None = db.query(UgcVariation).filter(UgcVariation.id == variation_id).first()
    if var is None:
        raise ValueError(f"variation {variation_id} not found")
    campaign = db.query(UgcCampaign).filter(UgcCampaign.id == var.campaign_id).first()
    workspace = (
        db.query(Workspace).filter(Workspace.id == campaign.workspace_id).first() if campaign else None
    )
    show_disclosure = disclosure_enabled(workspace.settings if workspace else None)

    def report(p: int) -> None:
        if on_progress:
            try:
                on_progress(p)
            except Exception:  # noqa: BLE001
                logger.exception("ugc progress callback failed")

    report(5)
    aspect = var.aspect_ratio or "9:16"
    w, h = ASPECT_RATIOS.get(aspect, ASPECT_RATIOS["9:16"])
    length = max(3, min(int(var.length_sec or 30), 60))

    provider = get_avatar_provider(var.provider)
    job = provider.start_render(
        script=var.script or var.hook or "",
        avatar_id=var.provider_avatar_id or "",
        voice_id=var.provider_voice_id or "",
        aspect_ratio=aspect,
        length_sec=length,
    )
    var.provider_job_id = job.provider_job_id
    db.commit()
    report(20)

    video_url = job.video_url
    if not video_url:
        import time

        deadline = time.time() + float(os.environ.get("UGC_PROVIDER_POLL_BUDGET_SEC", "1200"))
        interval = float(os.environ.get("UGC_PROVIDER_POLL_SEC", "15"))
        while time.time() < deadline:
            status = provider.poll(job.provider_job_id)
            if status.status == "done" and status.video_url:
                video_url = status.video_url
                break
            if status.status == "failed":
                raise RuntimeError(status.error or "provider render failed")
            time.sleep(interval)
        if not video_url:
            raise RuntimeError("provider render timed out")
    report(45)

    UGC_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_dir = UGC_OUTPUT_DIR / str(var.id)
    out_dir.mkdir(parents=True, exist_ok=True)
    disclosure_ok = False

    with tempfile.TemporaryDirectory(prefix=f"ugc-{var.id}-") as tmp:
        tmp_path = Path(tmp)
        source = _materialize_source(video_url, tmp_path / "source.mp4")
        report(55)

        scaled = tmp_path / "scaled.mp4"
        _scale_crop(source, aspect, scaled)
        # Real providers' clip length = TTS duration, not our advisory length_sec —
        # probe it so disclosure/hook/CTA overlay windows land correctly.
        dur = _probe_duration(scaled) or float(length)
        report(70)

        composed = tmp_path / "composed.mp4"
        try:
            applied = _composite_overlays(
                scaled, composed, w=w, h=h, duration=dur,
                hook=var.hook or "", cta=var.cta or "", disclosure=show_disclosure, work=tmp_path,
            )
            disclosure_ok = bool(show_disclosure and applied)
            final = composed
        except Exception:  # noqa: BLE001 — overlays are best-effort
            logger.exception("ugc overlay compositing failed for variation %s; using clean cut", var.id)
            final = scaled

        output_file = out_dir / "output.mp4"
        shutil.move(str(final), output_file)
        thumb_file = out_dir / "thumbnail.jpg"
        try:
            _extract_thumbnail(output_file, thumb_file, t=min(1.0, dur / 2.0))
        except Exception:  # noqa: BLE001
            logger.exception("ugc thumbnail failed for variation %s", var.id)
        report(90)

    public_path = f"{UGC_PUBLIC_PREFIX.rstrip('/')}/{var.id}/output.mp4"
    thumb_public = f"{UGC_PUBLIC_PREFIX.rstrip('/')}/{var.id}/thumbnail.jpg"
    folder = os.environ.get("CLOUDINARY_UGC_FOLDER", "aiugc").strip().strip("/")

    if not _dry_run() and cloudinary_credentials_configured():
        try:
            import app.utils.cloudinary  # noqa: F401 — ensure cloudinary.config runs

            var.storage_url = upload_local_path_to_cloudinary(
                output_file, resource_type="video", folder=folder, public_id=f"{var.id}/output"
            )
            if thumb_file.exists():
                var.thumbnail_url = upload_local_path_to_cloudinary(
                    thumb_file, resource_type="image", folder=folder, public_id=f"{var.id}/thumbnail"
                )
            else:
                var.thumbnail_url = thumb_public
        except Exception:  # noqa: BLE001
            logger.exception("ugc variation %s Cloudinary upload failed; using local paths", var.id)
            var.storage_url = public_path
            var.thumbnail_url = thumb_public
    else:
        var.storage_url = public_path
        var.thumbnail_url = thumb_public

    var.disclosure_applied = disclosure_ok
    db.commit()
    report(100)
    return str(var.storage_url or public_path)
