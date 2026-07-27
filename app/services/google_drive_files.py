"""Google Drive file metadata + validation gates for wizard imports.

Everything here runs under the ``drive.file`` scope, so every call only works
for files the user explicitly picked through the Google Picker.

All Drive calls pass ``supportsAllDrives=True`` so files living in Shared
Drives behave identically to My Drive files.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

MAX_FILE_SIZE_MB = int(os.getenv("DRIVE_IMPORT_MAX_FILE_SIZE_MB", "10240"))

_GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."
_SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

# Fields we need: identity, gating data, and the metadata that lets the wizard
# show a thumbnail + enable the trim step before any bytes have transferred.
_FILE_FIELDS = (
    "id,name,mimeType,size,thumbnailLink,iconLink,webViewLink,owners(displayName,emailAddress),"
    "videoMediaMetadata(durationMillis,width,height),capabilities(canDownload),"
    "shortcutDetails(targetId,targetMimeType),driveId,trashed"
)


class DriveFileError(Exception):
    """A Drive file cannot be imported. ``code`` is machine-readable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class DriveFileMeta:
    file_id: str
    name: str
    mime_type: str
    size_bytes: int = 0
    duration_seconds: int = 0
    width: int = 0
    height: int = 0
    thumbnail_url: str | None = None
    owner_name: str | None = None
    owner_email: str | None = None
    can_download: bool = True
    drive_id: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_payload(self) -> dict:
        return {
            "file_id": self.file_id,
            "name": self.name,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "duration_seconds": self.duration_seconds,
            "width": self.width,
            "height": self.height,
            "thumbnail_url": self.thumbnail_url,
            "owner_name": self.owner_name,
            "owner_email": self.owner_email,
        }


def build_drive_service(creds):
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _get_raw(service, file_id: str) -> dict:
    return (
        service.files()
        .get(fileId=file_id, fields=_FILE_FIELDS, supportsAllDrives=True)
        .execute()
    )


def fetch_file_metadata(service, file_id: str) -> DriveFileMeta:
    """Read Drive metadata, resolving shortcuts to their target.

    Raises ``DriveFileError`` for anything the user needs to be told about.
    """
    try:
        raw = _get_raw(service, file_id)
        # A picked shortcut points at the real file; follow it once.
        if raw.get("mimeType") == _SHORTCUT_MIME:
            target_id = (raw.get("shortcutDetails") or {}).get("targetId")
            if not target_id:
                raise DriveFileError("shortcut_unresolved", "This Drive shortcut doesn't point to a file.")
            raw = _get_raw(service, target_id)
    except HttpError as e:
        status = getattr(getattr(e, "resp", None), "status", None)
        if status in (401, 403):
            raise DriveFileError(
                "reauth_required", "Google Drive access expired. Reconnect the account."
            ) from e
        if status == 404:
            raise DriveFileError(
                "not_found", "That file is no longer available in Google Drive."
            ) from e
        logger.warning("Drive files.get failed for %s: %s", file_id, e)
        raise DriveFileError("drive_error", "Couldn't read that file from Google Drive.") from e

    if raw.get("trashed"):
        raise DriveFileError("trashed", "That file is in the Google Drive trash.")

    mime = (raw.get("mimeType") or "").strip()

    # Gate 2 — Google-native docs have no importable media bytes.
    if mime.startswith(_GOOGLE_NATIVE_PREFIX):
        raise DriveFileError(
            "google_native", "Google Docs, Sheets and Slides files can't be imported."
        )

    # Gate 1 — must be media. Audio is allowed: the wizard accepts audio/* too.
    if not (mime.startswith("video/") or mime.startswith("audio/")):
        raise DriveFileError(
            "not_media", "Pick a video or audio file — that file type isn't supported."
        )

    # Gate 5 — the owner may have disabled downloading.
    caps = raw.get("capabilities") or {}
    if caps.get("canDownload") is False:
        raise DriveFileError(
            "download_disabled", "The owner of this file has disabled downloading."
        )

    size_bytes = int(raw.get("size") or 0)

    # Gate 4 — size ceiling.
    if size_bytes and size_bytes > MAX_FILE_SIZE_MB * 1024 * 1024:
        limit = (
            f"{MAX_FILE_SIZE_MB // 1024} GB"
            if MAX_FILE_SIZE_MB >= 1024
            else f"{MAX_FILE_SIZE_MB} MB"
        )
        raise DriveFileError("too_large", f"That file is larger than the {limit} import limit.")

    vmm = raw.get("videoMediaMetadata") or {}
    duration_ms = int(vmm.get("durationMillis") or 0)
    owners = raw.get("owners") or []
    owner = owners[0] if owners else {}

    meta = DriveFileMeta(
        file_id=str(raw.get("id") or file_id),
        name=(raw.get("name") or "Untitled").strip()[:255],
        mime_type=mime,
        size_bytes=size_bytes,
        duration_seconds=int(duration_ms / 1000) if duration_ms else 0,
        width=int(vmm.get("width") or 0),
        height=int(vmm.get("height") or 0),
        thumbnail_url=raw.get("thumbnailLink"),
        owner_name=owner.get("displayName"),
        owner_email=owner.get("emailAddress"),
        can_download=caps.get("canDownload", True),
        drive_id=raw.get("driveId"),
    )

    # Drive reports no duration for some containers (e.g. MKV) — the import job
    # ffprobes as a fallback, so this is a note, not a failure.
    if meta.duration_seconds == 0 and mime.startswith("video/"):
        meta.warnings.append("duration_unknown")

    return meta
