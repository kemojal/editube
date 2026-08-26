"""Storage backend contract + shared key/content-type helpers.

A ``StorageBackend`` is a pluggable object store. Implementations: R2 (default
for hosted), Cloudinary (legacy / rollback), Local disk (self-hosted / dev).
See ``docs/r2-storage-migration-plan.md``.
"""
from __future__ import annotations

import mimetypes
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol, runtime_checkable

# Cloudinary "resource_type" -> a sane default content-type when the filename
# gives us nothing. Kept so callers can pass the old resource_type vocabulary.
_RESOURCE_DEFAULT_CT = {
    "video": "video/mp4",
    "image": "image/jpeg",
    "raw": "application/octet-stream",
}


@dataclass
class UploadResult:
    """Uniform result across backends. ``key`` is the object key (R2/local) or
    Cloudinary ``public_id``; stored only when a caller wants lifecycle later."""

    url: str
    bytes: int
    key: str
    content_type: str


@dataclass(frozen=True)
class PresignedUpload:
    """A short-lived browser-to-storage upload destination."""

    upload_url: str
    public_url: str
    key: str
    headers: dict[str, str]


@runtime_checkable
class StorageBackend(Protocol):
    """Write-only object store (we never delete today — see migration plan §2)."""

    name: str

    def available(self) -> bool:
        """True if this backend has the config it needs to upload."""

    def upload_stream(self, fileobj: BinaryIO, *, key: str, content_type: str) -> UploadResult:
        ...

    def upload_path(self, path: str | Path, *, key: str, content_type: str) -> UploadResult:
        ...

    def upload_bytes(self, data: bytes, *, key: str, content_type: str) -> UploadResult:
        ...

    def public_url(self, key: str) -> str:
        ...


def guess_content_type(
    filename: str | None,
    *,
    resource_type: str = "video",
    fallback: str | None = None,
) -> str:
    """Best-effort MIME type from a filename, falling back to the Cloudinary-style
    ``resource_type`` default. Keeps callers that only know ``resource_type`` working."""
    if filename:
        ct, _ = mimetypes.guess_type(filename)
        if ct:
            return ct
    if fallback:
        return fallback
    return _RESOURCE_DEFAULT_CT.get(resource_type, "application/octet-stream")


def _ext_for(filename: str | None, content_type: str | None) -> str:
    """Pick a file extension from filename, else from content-type. '' if unknown."""
    if filename:
        suffix = Path(filename).suffix
        if suffix:
            return suffix
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if guessed:
            return guessed
    return ""


def build_key(
    *,
    folder: str = "",
    public_id: str | None = None,
    filename: str | None = None,
    content_type: str | None = None,
) -> str:
    """Compose an object key ``<folder>/<public_id-or-uuid><ext>``.

    - ``public_id`` mirrors Cloudinary's caller-supplied id (e.g. ``"<clip>/thumbnail"``);
      when given it is used verbatim (no random component) for idempotent overwrites.
    - Without ``public_id`` a uuid is generated so uploads never collide.
    - Extension is derived from filename or content-type so public URLs are typed.
    """
    folder = (folder or "").strip().strip("/")
    if public_id:
        stem = public_id.strip().strip("/")
        # public_id may already carry an extension; only append if it lacks one.
        ext = "" if Path(stem).suffix else _ext_for(filename, content_type)
    else:
        stem = uuid.uuid4().hex
        ext = _ext_for(filename, content_type)
    key = f"{stem}{ext}"
    return f"{folder}/{key}" if folder else key
