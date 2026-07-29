"""
RQ job: concatenate rough-cut keepRanges via ffmpeg and upload to Cloudinary.

Supports: frameRate from exportSettings, optional burned-in subtitles from DB
transcription + keepRanges, optional 9:16 shorts crop, and metadata when
lower-thirds / brand burn-in is requested but not yet implemented.
"""

from __future__ import annotations

import logging
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
        # I11: tracked across segments -- if a mask was requested but any
        # segment's matte failed to render, the export still finishes
        # (fail-open: a masking bug must not fail an entire export) but the
        # UI needs to know the result is unmasked, since a mask can be a
        # redaction, not just decoration.
        mask_render_failed_for_segment = False
        matte_fps = _resolve_numeric_fps(settings, media_src) if (want_mp4_video and masks) else 30.0
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
                    vf = (
                        f"scale={scale}:force_original_aspect_ratio=decrease,"
                        f"pad={scale}:(ow-iw)/2:(oh-ih)/2"
                        f"{fps_extra}"
                    )

                    matte_path: Path | None = None
                    if masks:
                        try:
                            matte_path = render_matte_video(
                                masks,
                                duration=dur,
                                fps=matte_fps,
                                size=(scale_w, scale_h),
                                out_path=tmp_path / f"matte{index:03d}.mkv",
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

                    if matte_path is not None:
                        filter_complex = (
                            f"[0:v]{vf}[base];"
                            f"[base][1:v]alphamerge[m];"
                            f"color=black:s={scale}[bg];"
                            f"[bg][m]overlay=shortest=1[v]"
                        )
                        _run_ffmpeg(
                            [
                                "ffmpeg",
                                "-y",
                                "-ss",
                                str(start),
                                "-i",
                                media_src,
                                "-i",
                                str(matte_path),
                                "-t",
                                str(dur),
                                "-filter_complex",
                                filter_complex,
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
                                "aac",
                                "-b:a",
                                "192k",
                                "-movflags",
                                "+faststart",
                                str(v_out),
                            ]
                        )
                    else:
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
                                "-vf",
                                vf,
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
                                str(v_out),
                            ]
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
