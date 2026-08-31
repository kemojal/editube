#!/usr/bin/env python
"""Backfill editing proxies and waveform peaks for videos that predate them.

Both optimisations are produced at ingest time — proxies when a video is
uploaded, peaks while it is transcribed — so footage that already existed
never got either. Until it does, the editor keeps playing the full-resolution
master and keeps falling back to downloading the whole file in the browser
just to draw the waveform.

    python scripts/backfill_media.py status
    python scripts/backfill_media.py peaks   [--project-id 5] [--video-ids 4,7] [--limit N] [--dry-run]
    python scripts/backfill_media.py proxies [--project-id 5] [--video-ids 4,7] [--limit N] [--dry-run]

Peaks are cheap: ffmpeg streams the audio track and Silero runs over it — no
re-transcription, and existing speech/silence ranges are preserved. Proxies
are expensive: each one is a full transcode uploaded to object storage, so
start with the projects you actually edit.

Proxies are enqueued onto the RQ queue, so the worker must be running for
them to make progress. Everything is resumable — a re-run only picks up what
is still missing.
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import not_, or_  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db.database import SessionLocal  # noqa: E402
from app.db.models import Video, VideoProxy, VideoTranscription  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("backfill")


# Imported stills live in the videos table too (imported as "Rough cut
# asset"). They have no audio track to draw a waveform from and nothing to
# transcode, so counting them as work left to do — or queueing a proxy
# transcode of a PNG — is just noise.
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".avif")


def _is_image(file_path: str | None) -> bool:
    return (file_path or "").split("?")[0].lower().endswith(_IMAGE_SUFFIXES)


def _selected_video_ids(args: argparse.Namespace) -> list[int] | None:
    if args.video_ids:
        return [int(v) for v in args.video_ids.split(",") if v.strip()]
    return None


def _apply_scope(query, args: argparse.Namespace):
    ids = _selected_video_ids(args)
    if ids:
        query = query.filter(Video.id.in_(ids))
    if args.project_id:
        query = query.filter(Video.project_id == args.project_id)
    return query


def cmd_status(db: Session, args: argparse.Namespace) -> int:
    rows = _apply_scope(db.query(Video.id, Video.file_path), args).all()
    media_ids = {vid for vid, path in rows if not _is_image(path)}
    images = len(rows) - len(media_ids)
    if not media_ids:
        logger.info("No audio/video in scope (%d image asset(s)).", images)
        return 0

    with_proxy = (
        db.query(VideoProxy.video_id)
        .filter(VideoProxy.video_id.in_(media_ids), VideoProxy.status == "completed")
        .distinct()
        .count()
    )
    with_peaks = (
        db.query(VideoTranscription.video_id)
        .filter(
            VideoTranscription.video_id.in_(media_ids),
            VideoTranscription.audio_analysis.has_key("peaks"),  # noqa: W601
        )
        .count()
    )
    total = len(media_ids)
    logger.info("audio/video:       %d  (plus %d image asset(s), not applicable)", total, images)
    logger.info("with proxy:        %d  (missing %d)", with_proxy, total - with_proxy)
    logger.info("with peaks:        %d  (missing %d)", with_peaks, total - with_peaks)
    return 0


def _describe_source_failure(exc: Exception, file_path: str | None) -> str:
    """Say why a source could not be read, rather than echoing the ffmpeg call.

    The two failures that actually occur are a YouTube stream URL that has
    expired and a local path that no longer exists, and both are worth naming:
    the first is fixable by refreshing the stream, the second never will be.
    """
    import subprocess

    src = file_path or ""
    if src.startswith("/") and not Path(src).exists():
        return f"source file is gone ({src})"
    if "googlevideo.com" in src:
        return "YouTube stream URL has expired — refresh the stream, then re-run"
    if isinstance(exc, subprocess.CalledProcessError):
        from app.jobs.transcription import _ffmpeg_stderr_suggests_no_audio

        stderr = exc.stderr or ""
        # With -vn the output only fails to open when the demuxed input left
        # nothing to write, i.e. the source carries no audio stream at all.
        if _ffmpeg_stderr_suggests_no_audio(stderr) or "opening output" in stderr.lower():
            return "no audio track — nothing to draw a waveform from"
        lines = stderr.strip().splitlines()
        return lines[-1][:160] if lines else "ffmpeg could not read the source"
    if isinstance(exc, subprocess.TimeoutExpired):
        return "ffmpeg timed out reading the source"
    return str(exc)[:160]


def cmd_peaks(db: Session, args: argparse.Namespace) -> int:
    from app.jobs.transcription import _run_ffmpeg_to_wav
    from app.services.audio_analysis import analyze_wav_speech

    query = _apply_scope(
        db.query(Video.id, Video.name, Video.file_path).join(
            VideoTranscription, VideoTranscription.video_id == Video.id
        ),
        args,
    ).filter(Video.file_path.isnot(None))
    if not args.force:
        query = query.filter(
            or_(
                VideoTranscription.audio_analysis.is_(None),
                not_(VideoTranscription.audio_analysis.has_key("peaks")),  # noqa: W601
            )
        )
    query = query.order_by(Video.id)
    if args.limit:
        query = query.limit(args.limit)
    targets = query.all()
    images = sum(1 for _, _, path in targets if _is_image(path))
    targets = [row for row in targets if not _is_image(row[2])]
    if images:
        logger.info("Ignoring %d image asset(s) — no audio to analyse.", images)

    if not targets:
        logger.info("Nothing to do — every selected video already has peaks.")
        return 0
    logger.info("%d video(s) need peaks.", len(targets))
    if args.dry_run:
        for video_id, name, _ in targets:
            logger.info("  would process %s  %s", video_id, name)
        return 0

    done = failed = 0
    for video_id, name, file_path in targets:
        logger.info("[%s] %s", video_id, name)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                wav_path = Path(tmp) / "audio.wav"
                # ffmpeg streams the input, so this never downloads the video.
                _run_ffmpeg_to_wav(file_path, wav_path)
                analysis = analyze_wav_speech(wav_path)
            if not analysis or not analysis.get("peaks"):
                logger.warning("  no peaks produced (silent or unreadable audio)")
                failed += 1
                continue

            row = (
                db.query(VideoTranscription)
                .filter(VideoTranscription.video_id == video_id)
                .first()
            )
            if row is None:
                logger.warning("  transcription row vanished; skipping")
                failed += 1
                continue

            # Merge rather than replace: a row may already carry speech ranges
            # the editor's silence UI is built on, and re-running the VAD would
            # shift them for no reason. JSONB needs a fresh object to be seen
            # as dirty, so this reassigns instead of mutating in place.
            existing = row.audio_analysis if isinstance(row.audio_analysis, dict) else {}
            row.audio_analysis = {**analysis, **existing, "peaks": analysis["peaks"]}
            db.commit()
            logger.info("  %d peak buckets stored", len(analysis["peaks"]))
            done += 1
        except Exception as exc:  # noqa: BLE001 — one bad file must not stop the batch
            db.rollback()
            logger.warning("  skipped: %s", _describe_source_failure(exc, file_path))
            failed += 1

    logger.info("peaks backfill complete: %d done, %d skipped", done, failed)
    if failed:
        logger.info("Skipped sources are unreadable, not errors here — see the reasons above.")
    return 0


def cmd_proxies(db: Session, args: argparse.Namespace) -> int:
    from app.services.proxy_service import DEFAULT_PROFILE, create_proxy

    have = {
        row[0]
        for row in db.query(VideoProxy.video_id)
        .filter(
            VideoProxy.profile == DEFAULT_PROFILE,
            VideoProxy.status.in_(("completed", "pending", "processing")),
        )
        .all()
    }
    query = _apply_scope(
        db.query(Video.id, Video.name, Video.file_path), args
    ).filter(Video.file_path.isnot(None))
    if not args.force and have:
        query = query.filter(~Video.id.in_(have))
    query = query.order_by(Video.id)
    if args.limit:
        query = query.limit(args.limit)
    targets = query.all()
    images = sum(1 for _, _, path in targets if _is_image(path))
    targets = [(vid, name) for vid, name, path in targets if not _is_image(path)]
    if images:
        logger.info("Ignoring %d image asset(s) — nothing to transcode.", images)

    if not targets:
        logger.info("Nothing to do — every selected video already has a proxy.")
        return 0
    logger.info(
        "%d video(s) need a %s proxy. Each is a full transcode uploaded to "
        "storage, and the RQ worker must be running.",
        len(targets),
        DEFAULT_PROFILE,
    )
    if args.dry_run:
        for video_id, name in targets:
            logger.info("  would enqueue %s  %s", video_id, name)
        return 0

    queued = skipped = 0
    for video_id, name in targets:
        try:
            create_proxy(db, video_id, DEFAULT_PROFILE)
            logger.info("[%s] queued  %s", video_id, name)
            queued += 1
        except ValueError as exc:
            logger.info("[%s] skipped (%s)", video_id, exc)
            skipped += 1
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            logger.warning("[%s] failed: %s", video_id, str(exc)[:200])
            skipped += 1

    logger.info("%d queued, %d skipped. Watch the RQ worker for progress.", queued, skipped)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("status", "show proxy and peak coverage"),
        ("peaks", "compute waveform peaks for videos missing them"),
        ("proxies", "enqueue proxy generation for videos missing one"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--project-id", type=int, help="limit to one project")
        p.add_argument("--video-ids", help="comma-separated video ids")
        p.add_argument("--limit", type=int, help="process at most N videos")
        p.add_argument("--dry-run", action="store_true", help="list targets, change nothing")
        p.add_argument("--force", action="store_true", help="include videos that already have one")

    args = parser.parse_args()
    handlers = {"status": cmd_status, "peaks": cmd_peaks, "proxies": cmd_proxies}
    db = SessionLocal()
    try:
        return handlers[args.command](db, args)
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
