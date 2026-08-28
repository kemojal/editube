from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from app.db.database import SessionLocal
from app.db.models import AiResult, Video
from app.services.chroma_key import build_chroma_key_filter, chroma_key_from_attributes
from app.services.color_adjust import build_adjust_filter_chain
from app.services.segmentation import get_provider
from app.utils.cloudinary import cloudinary_credentials_configured, upload_local_path_to_cloudinary


FFMPEG_EFFECTS = {"speed", "audio", "adjust"}
ML_EFFECTS = {"remove_bg", "voice_changer", "ai_stylize", "video_enhance", "mask"}


def build_ffmpeg_effect_command(
    input_path: str,
    output_path: str,
    *,
    effect_type: str,
    clip_target: dict[str, Any],
    settings: dict[str, Any],
) -> list[str]:
    start = _num(clip_target.get("start"), 0)
    end = _num(clip_target.get("end"), 0)
    duration = max(0.05, end - start) if end > start else 0
    cmd = ["ffmpeg", "-y"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", input_path]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]

    vf: list[str] = []
    af: list[str] = []
    audio_only = clip_target.get("track") == "audio"

    if effect_type == "speed":
        rate = max(0.05, _num(settings.get("rate"), 1))
        if not audio_only:
            vf.append(f"setpts=PTS/{rate:.5f}")
        af.extend(_atempo_chain(rate))
    elif effect_type == "audio":
        volume = _num(settings.get("volume"), 0)
        fade_in = max(0, _num(settings.get("fadeIn"), 0))
        fade_out = max(0, _num(settings.get("fadeOut"), 0))
        af.append(f"volume={volume:.3f}dB")
        if fade_in > 0:
            af.append(f"afade=t=in:st=0:d={fade_in:.3f}")
        if fade_out > 0 and duration > 0:
            af.append(f"afade=t=out:st={max(0, duration - fade_out):.3f}:d={fade_out:.3f}")
    elif effect_type == "adjust":
        vf.extend(build_adjust_filter_chain(settings))

    if vf:
        cmd += ["-vf", ",".join(vf)]
    if af:
        cmd += ["-af", ",".join(af)]

    cmd += [
        "-map",
        "0:v?",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        output_path,
    ]
    return cmd


def build_chroma_key_command(
    source: str, out_path: str, clip_target: dict[str, Any], settings: dict[str, Any]
) -> list[str]:
    """ffmpeg command for a chroma-key-only removal.

    The filter itself comes from `app.services.chroma_key`, which is held to the
    same golden fixture as the frontend keyer — see tests/test_chroma_key.py.

    VP9 in WebM with yuva420p because the output is a cutout: h264 has no alpha,
    so an mp4 here would look correct in isolation and be wrong the moment it is
    composited.
    """
    chroma = chroma_key_from_attributes(settings)
    chain = build_chroma_key_filter(chroma)
    if not chain:
        raise RuntimeError("Chroma key is enabled but no key colour is set.")

    # Trim to the clip, like every other effect here. Keying the whole source
    # would burn minutes of encode for a few seconds of output.
    start = _num(clip_target.get("start"), 0)
    end = _num(clip_target.get("end"), 0)
    duration = max(0.05, end - start) if end > start else 0

    return [
        "ffmpeg", "-y",
        *(["-ss", f"{start:.3f}"] if start > 0 else []),
        "-i", source,
        *(["-t", f"{duration:.3f}"] if duration else []),
        "-vf", chain,
        "-c:v", "libvpx-vp9",
        "-pix_fmt", "yuva420p",
        "-b:v", "0", "-crf", "30",
        # The processed layer is visual-only; the original media remains the
        # playback/audio clock. Copying common AAC audio into WebM is invalid
        # and made chroma-only removal fail at the final mux step.
        "-an",
        out_path,
    ]


def rough_cut_effect_job(ai_result_id: int) -> None:
    db = SessionLocal()
    try:
        row = db.query(AiResult).filter(AiResult.id == ai_result_id).first()
        if row is None or row.result_type != "rough_cut_effect":
            raise RuntimeError(f"Rough-cut effect {ai_result_id} was removed before processing")
        payload = dict(row.result_data or {})
        video = db.query(Video).filter(Video.id == row.video_id).first()
        if video is None:
            raise RuntimeError("Video not found")

        effect_type = str(payload.get("effectType") or "").strip().lower()
        clip_target = payload.get("clipTarget") if isinstance(payload.get("clipTarget"), dict) else {}
        settings = payload.get("settings") if isinstance(payload.get("settings"), dict) else {}
        clip_key = str(payload.get("clipKey") or "").strip()

        _update_row(db, row, status="processing", progress=8)
        source = _resolve_media_source(video.file_path)
        run_target = dict(clip_target)
        run_settings = dict(settings)
        if effect_type == "adjust" and run_settings.get("lut"):
            from app.services.lut import resolve_adjust_lut, video_workspace_ids

            resolve_adjust_lut(
                db, run_settings, allowed_workspace_ids=video_workspace_ids(db, video)
            )

        # Beauty is intentionally before background removal in the visual
        # stack. If this clip has an approved completed retouch, use that
        # clip-only file as the removal source and translate the source-time
        # target/prompt to its zero-based timeline.
        if effect_type == "remove_bg":
            retouched = _completed_effect_source(db, row.video_id, clip_key, "retouch")
            if retouched:
                source = retouched
                original_start = max(0.0, _num(clip_target.get("start"), 0))
                original_end = _num(clip_target.get("end"), original_start)
                run_target["start"] = 0.0
                run_target["end"] = max(0.05, original_end - original_start)
                if "selectionAnchorSeconds" in run_settings:
                    run_settings["selectionAnchorSeconds"] = max(
                        0.0,
                        _num(run_settings.get("selectionAnchorSeconds"), original_start) - original_start,
                    )

        # Chroma key needs no model — it is a pure ffmpeg filter. A removal that
        # only keys should therefore work on any server, with no provider
        # configured and nothing installed. Only auto/custom removal, which do
        # need a segmentation model, go to a provider.
        chroma_only = (
            effect_type == "remove_bg"
            and bool(settings.get("chromaKey"))
            and not settings.get("autoRemoval")
            and not settings.get("customRemoval")
        )

        if effect_type == "retouch":
            from app.services.retouch import render_retouch_video

            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / f"rough-cut-effect-{row.id}.mp4"
                render_retouch_video(
                    source,
                    clip_target,
                    settings,
                    out,
                    progress=lambda value: _update_row(db, row, status="processing", progress=value),
                    cancel=lambda: _was_canceled(db, row),
                )
                output_url = _publish_output(out, row.video_id, row.id)
            _complete(db, row, clip_key, effect_type, output_url)
            return

        if effect_type == "audio":
            from app.services.audio_enhancement import render_audio_enhancement

            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / f"rough-cut-effect-{row.id}.m4a"
                result = render_audio_enhancement(
                    source,
                    run_target,
                    run_settings,
                    out,
                    progress=lambda value: _update_row(db, row, status="processing", progress=value),
                )
                output_url = _publish_output(result.path, row.video_id, row.id)
            _complete(
                db,
                row,
                clip_key,
                effect_type,
                output_url,
                metadata={"provider": result.provider},
            )
            return

        if effect_type in ML_EFFECTS and not chroma_only:
            # Providers produce a file or a URL; publishing stays here, because
            # this is what already knows Cloudinary, the uploads directory and
            # the naming scheme.
            provider = get_provider()
            with tempfile.TemporaryDirectory() as tmp:
                result = provider.run_effect(
                    source,
                    effect_type,
                    run_target,
                    run_settings,
                    output_dir=Path(tmp),
                    progress=lambda value: _update_row(db, row, status="processing", progress=value),
                )
                output_url = (
                    result.url
                    if result.url
                    else _publish_output(result.path, row.video_id, row.id)
                )
            _complete(db, row, clip_key, effect_type, output_url)
            return

        if effect_type not in FFMPEG_EFFECTS and not chroma_only:
            raise RuntimeError(f"Unsupported effect type: {effect_type}")

        with tempfile.TemporaryDirectory() as tmp:
            # A keyed result has to keep its alpha, so it cannot be h264/mp4 —
            # that would silently composite the cutout onto black.
            suffix = "webm" if chroma_only else "mp4"
            out = Path(tmp) / f"rough-cut-effect-{row.id}.{suffix}"
            if chroma_only:
                cmd = build_chroma_key_command(source, str(out), run_target, run_settings)
            else:
                cmd = build_ffmpeg_effect_command(
                    source,
                    str(out),
                    effect_type=effect_type,
                    clip_target=clip_target,
                    settings=settings,
                )
            _update_row(db, row, status="processing", progress=20)
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            _update_row(db, row, status="processing", progress=84)
            output_url = _publish_output(out, row.video_id, row.id)
            _complete(db, row, clip_key, effect_type, output_url)
    except Exception as exc:
        if "row" in locals() and row is not None:
            if _was_canceled(db, row):
                return
            _fail(db, row, str(exc))
        raise
    finally:
        db.close()


def _completed_effect_source(
    db: Any,
    video_id: int,
    clip_key: str,
    effect_type: str,
) -> str | None:
    """Resolve an earlier visual effect only from completed owned result rows."""
    rows = (
        db.query(AiResult)
        .filter(
            AiResult.video_id == video_id,
            AiResult.result_type == "rough_cut_effect",
        )
        .all()
    )
    matches: list[Any] = []
    for candidate in rows:
        data = candidate.result_data if isinstance(candidate.result_data, dict) else {}
        if (
            data.get("effectType") == effect_type
            and data.get("clipKey") == clip_key
        ):
            matches.append(candidate)
    if not matches:
        return None
    latest = max(matches, key=lambda item: int(item.id))
    data = latest.result_data if isinstance(latest.result_data, dict) else {}
    url = data.get("outputUrl")
    if latest.status != "completed" or not isinstance(url, str) or not url.strip():
        return None
    return _resolve_media_source(url.strip())


# `_run_ml_provider` moved to app/services/segmentation/http.py, which speaks
# the same ROUGH_CUT_ML_PROVIDER_URL contract. Selection is now
# SEGMENTATION_PROVIDER=auto|local|http.


def _was_canceled(db, row: AiResult) -> bool:
    """True if the user cancelled while this job was running.

    Re-read from the database rather than trusted from memory: the cancel is
    written by the API process, so the copy this job loaded at the start cannot
    know about it. Without this check a job that finished in the gap between the
    stop being requested and delivered would write `completed` over the user's
    cancel, and the effect would appear to have applied anyway.
    """
    db.expire(row)
    return bool((row.result_data or {}).get("canceled")) or row.status == "canceled"


def _complete(
    db,
    row: AiResult,
    clip_key: str,
    effect_type: str,
    output_url: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> None:
    if _was_canceled(db, row):
        return
    _update_row(db, row, status="completed", progress=100, output_url=output_url)
    extra = dict(metadata or {})
    if extra:
        payload = dict(row.result_data or {})
        payload.update(extra)
        row.result_data = payload
        db.commit()
    _attach_to_draft(db, row.video_id, clip_key, effect_type, {
        "resultId": row.id,
        "status": "completed",
        "progress": 100,
        "outputUrl": output_url,
        **extra,
    })


def _fail(db, row: AiResult, message: str) -> None:
    # A cancelled job dies by signal or raises on a closed pipe; reporting that as
    # a failure would show the user an error for something they chose to do.
    if _was_canceled(db, row):
        return
    payload = dict(row.result_data or {})
    payload["status"] = "failed"
    payload["progress"] = 0
    payload["error"] = message
    row.status = "failed"
    row.error_message = message
    row.result_data = payload
    db.commit()
    clip_key = str(payload.get("clipKey") or "").strip()
    effect_type = str(payload.get("effectType") or "").strip()
    if clip_key and effect_type:
        _attach_to_draft(db, row.video_id, clip_key, effect_type, {
            "resultId": row.id,
            "status": "failed",
            "progress": 0,
            "error": message,
        })


def _update_row(db, row: AiResult, *, status: str, progress: int, output_url: str | None = None) -> None:
    payload = dict(row.result_data or {})
    payload["status"] = status
    payload["progress"] = progress
    if output_url:
        payload["outputUrl"] = output_url
    row.status = status
    row.result_data = payload
    row.error_message = None if status != "failed" else row.error_message
    db.commit()


def _attach_to_draft(db, video_id: int, clip_key: str, effect_type: str, processing: dict[str, Any]) -> None:
    draft = (
        db.query(AiResult)
        .filter(AiResult.video_id == video_id, AiResult.result_type == "rough_cut_draft")
        .first()
    )
    if draft is None:
        return
    data = dict(draft.result_data or {})
    clip_attrs = dict(data.get("clipAttributes") or {})
    attrs = dict(clip_attrs.get(clip_key) or {})
    current_processing = dict(attrs.get("processing") or {})
    if effect_type == "retouch":
        # Remove BG consumes the retouched visual. Its old matte cannot remain
        # authoritative after a new beauty pass finishes or fails.
        current_processing.pop("remove_bg", None)
    current_processing[effect_type] = processing
    attrs["processing"] = current_processing
    clip_attrs[clip_key] = attrs
    data["clipAttributes"] = clip_attrs
    draft.result_data = data
    draft.status = "completed"
    db.commit()


def _publish_output(path: Path, video_id: int, result_id: int) -> str:
    if cloudinary_credentials_configured():
        return upload_local_path_to_cloudinary(
            str(path),
            resource_type="video",
            folder=f"rough_cut_effects/{video_id}",
            public_id=f"effect_{result_id}",
        )
    uploads = Path(os.environ.get("UPLOADS_DIR", "./uploads")).resolve() / "rough_cut_effects" / str(video_id)
    uploads.mkdir(parents=True, exist_ok=True)
    # Keep the actual container extension. Background removal produces WebM
    # because browser-playable alpha needs VP9; renaming those bytes to `.mp4`
    # made StaticFiles send the wrong MIME type and many browsers refused to
    # decode the otherwise valid result.
    suffix = path.suffix.lower() if path.suffix.lower() in {".webm", ".mp4", ".mov", ".m4a"} else ".mp4"
    dest = uploads / f"effect_{result_id}{suffix}"
    shutil.copyfile(path, dest)
    return f"/uploads/rough_cut_effects/{video_id}/{dest.name}"


def _resolve_media_source(file_path: str) -> str:
    value = (file_path or "").strip()
    if value.startswith(("http://", "https://")):
        return value
    if value.startswith("/uploads/"):
        candidate = Path(os.environ.get("UPLOADS_DIR", "./uploads")).resolve() / value.removeprefix("/uploads/")
        if candidate.exists():
            return str(candidate)
    candidate = Path(value)
    if candidate.exists():
        return str(candidate)
    return value


def _atempo_chain(rate: float) -> list[str]:
    factors: list[float] = []
    current = rate
    while current > 2:
        factors.append(2)
        current /= 2
    while current < 0.5:
        factors.append(0.5)
        current /= 0.5
    factors.append(current)
    return [f"atempo={factor:.5f}" for factor in factors]


def _num(value: Any, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if number == number else fallback
