"""RQ job: stream a Google Drive file into our object storage.

Produces a storage URL in ``DriveImport.file_path``, which the create-project
wizard then hands to ``POST /projects/{id}/videos/from-upload`` — the same
contract as the stateless ``POST /upload/video``, so no submit-path changes are
needed. See docs/google-drive-import-plan.md §3.4.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from app.db.database import SessionLocal
from app.db.models import DriveImport
from app.services.google_drive_credentials import DriveReauthRequired, refresh_credentials_if_needed
from app.services.google_drive_files import build_drive_service, fetch_file_metadata
from app.storage import get_storage
from app.storage.base import build_key, guess_content_type

logger = logging.getLogger(__name__)

_CHUNK_BYTES = 8 * 1024 * 1024  # matches youtube_publish.py
# Progress is split so the bar never sits at 100% while the second transfer runs.
_DOWNLOAD_CEILING = 90
_COMMIT_MIN_PERCENT_DELTA = 2
_COMMIT_MIN_SECONDS = 2.0


def _video_folder() -> str:
    return os.getenv("STORAGE_VIDEO_FOLDER", "videos").strip()


def _fail(db, row: DriveImport, code: str, message: str) -> None:
    row.status = "failed"
    row.error_code = code
    row.error_message = message[:2000]
    db.add(row)
    db.commit()


def _is_canceled(db, import_id: int) -> bool:
    """Re-read only the status column so a cancel mid-transfer is seen promptly.

    A column query bypasses the identity map and emits a real SELECT, and the
    progress commits end the transaction regularly, so under Postgres READ
    COMMITTED each call sees a fresh snapshot. This would need rethinking under
    REPEATABLE READ.
    """
    status = (
        db.query(DriveImport.status).filter(DriveImport.id == import_id).scalar()
    )
    return status == "canceled"


def drive_import_job(import_id: int) -> None:
    db = SessionLocal()
    tmp_path: str | None = None
    try:
        row = db.query(DriveImport).filter(DriveImport.id == import_id).first()
        if not row:
            logger.warning("drive_import_job: import %s not found", import_id)
            return
        if row.status in ("completed", "canceled"):
            logger.info("drive_import_job: import %s already %s", import_id, row.status)
            return

        connection = row.connection
        if not connection:
            _fail(db, row, "no_connection", "The Google Drive account was disconnected.")
            return

        try:
            creds = refresh_credentials_if_needed(db, connection)
        except DriveReauthRequired as e:
            _fail(db, row, "reauth_required", str(e))
            return
        except RuntimeError as e:
            # Missing TOKEN_ENCRYPTION_KEY / client config.
            _fail(db, row, "not_configured", str(e))
            return
        except ValueError as e:
            # decrypt_secret with the wrong key.
            _fail(db, row, "decrypt_failed", str(e))
            return

        service = build_drive_service(creds)

        # Re-read metadata at job time: the file may have been renamed, moved to
        # trash or had sharing revoked between /resolve and the worker picking
        # this up.
        try:
            meta = fetch_file_metadata(service, row.drive_file_id)
        except Exception as e:
            code = getattr(e, "code", "drive_error")
            message = getattr(e, "message", str(e))
            _fail(db, row, code, message)
            return

        row.status = "downloading"
        row.file_name = meta.name
        row.mime_type = meta.mime_type
        row.total_bytes = meta.size_bytes or row.total_bytes or 0
        if meta.duration_seconds:
            row.duration_seconds = meta.duration_seconds
        if meta.thumbnail_url:
            row.thumbnail_url = meta.thumbnail_url
        db.add(row)
        db.commit()

        total = int(row.total_bytes or 0)
        suffix = os.path.splitext(meta.name)[1] or ""

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name
            request = service.files().get_media(fileId=meta.file_id, supportsAllDrives=True)
            downloader = MediaIoBaseDownload(tmp, request, chunksize=_CHUNK_BYTES)

            last_commit_percent = 0
            last_commit_at = time.monotonic()
            done = False
            while not done:
                if _is_canceled(db, import_id):
                    logger.info("drive_import_job: import %s canceled mid-download", import_id)
                    return
                try:
                    status, done = downloader.next_chunk()
                except HttpError as e:
                    http_status = getattr(getattr(e, "resp", None), "status", None)
                    if http_status in (401, 403):
                        _fail(
                            db,
                            row,
                            "reauth_required",
                            "Google Drive access expired. Reconnect the account.",
                        )
                        return
                    logger.warning("Drive download failed for import %s: %s", import_id, e)
                    _fail(db, row, "download_failed", "Couldn't download that file from Google Drive.")
                    return

                if status is not None:
                    transferred = int(status.resumable_progress or 0)
                    # Derive percent from OUR known total rather than
                    # status.progress(), which returns 0.0 whenever the response
                    # carried no Content-Length — that would pin the bar at 0%
                    # for an entire transfer while bytes visibly climbed.
                    if total > 0:
                        percent = int(min(transferred / total, 1.0) * _DOWNLOAD_CEILING)
                    else:
                        percent = min(_DOWNLOAD_CEILING - 1, last_commit_percent + 1)
                    now = time.monotonic()
                    # Throttle writes: a 5 GB file is ~640 chunks and each commit
                    # is a round trip we don't need.
                    if (
                        percent - last_commit_percent >= _COMMIT_MIN_PERCENT_DELTA
                        or now - last_commit_at >= _COMMIT_MIN_SECONDS
                    ):
                        row.bytes_transferred = transferred
                        row.progress_percent = min(percent, _DOWNLOAD_CEILING)
                        db.add(row)
                        db.commit()
                        last_commit_percent = percent
                        last_commit_at = now

            tmp.flush()

        downloaded_bytes = os.path.getsize(tmp_path)
        if downloaded_bytes == 0:
            _fail(db, row, "empty_file", "That Drive file came back empty.")
            return

        if _is_canceled(db, import_id):
            logger.info("drive_import_job: import %s canceled before upload", import_id)
            return

        row.status = "uploading"
        row.bytes_transferred = downloaded_bytes
        row.total_bytes = row.total_bytes or downloaded_bytes
        row.progress_percent = _DOWNLOAD_CEILING
        db.add(row)
        db.commit()

        # Drive omits durationMillis for some containers (e.g. MKV) — probe the
        # local file before it goes away.
        if not row.duration_seconds:
            try:
                from app.services.ingest_service import _probe_media

                probed = _probe_media(tmp_path)
                if probed.get("duration"):
                    row.duration_seconds = int(probed["duration"])
            except Exception as e:
                logger.info("ffprobe fallback failed for import %s: %s", import_id, e)

        content_type = meta.mime_type or guess_content_type(meta.name, resource_type="video")
        key = build_key(folder=_video_folder(), filename=meta.name, content_type=content_type)
        try:
            result = get_storage().upload_path(tmp_path, key=key, content_type=content_type)
        except Exception as e:
            logger.exception("Storage upload failed for drive import %s: %s", import_id, e)
            _fail(db, row, "storage_upload_failed", "Couldn't save the imported file to storage.")
            return

        row.file_path = result.url
        row.total_bytes = int(result.bytes or downloaded_bytes)
        row.bytes_transferred = row.total_bytes
        row.progress_percent = 100

        # A cancel can land *during* a multi-GB storage upload; without this
        # re-check it would be overwritten by "completed" and the wizard would
        # attach a file the user already removed. The uploaded object is left in
        # place — the storage backends are intentionally write-only.
        if _is_canceled(db, import_id):
            logger.info(
                "drive_import_job: import %s canceled during upload; leaving orphan object %s",
                import_id,
                result.key,
            )
            return

        row.status = "completed"
        row.error_code = None
        row.error_message = None
        db.add(row)
        db.commit()
        logger.info("drive_import_job: import %s completed -> %s", import_id, result.url)

    except Exception as e:
        logger.exception("drive_import_job crashed for import %s: %s", import_id, e)
        try:
            row = db.query(DriveImport).filter(DriveImport.id == import_id).first()
            if row and row.status not in ("completed", "canceled"):
                _fail(db, row, "unexpected_error", str(e))
        except Exception:
            pass
    finally:
        # A leaked multi-gigabyte temp file per import is not acceptable.
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        db.close()
