"""Pluggable object storage. ``get_storage()`` returns the backend selected by
``STORAGE_BACKEND`` (``r2`` | ``cloudinary`` | ``local``; default ``cloudinary``).

    from app.storage import get_storage, build_key, guess_content_type

    key = build_key(folder="repurpose_clips", public_id=f"{clip.id}/thumbnail",
                    content_type="image/jpeg")
    result = get_storage().upload_path(dst, key=key, content_type="image/jpeg")
    clip.thumbnail_url = result.url
"""
from __future__ import annotations

import logging
import os
import threading

from .base import PresignedUpload, StorageBackend, UploadResult, build_key, guess_content_type

logger = logging.getLogger(__name__)

_DEFAULT_BACKEND = "cloudinary"  # safe default: nothing changes until R2 is flipped on
_lock = threading.Lock()
_instances: dict[str, StorageBackend] = {}


def _build(name: str) -> StorageBackend:
    if name == "r2":
        from .r2 import R2Backend

        return R2Backend()
    if name == "cloudinary":
        from .cloudinary_backend import CloudinaryBackend

        return CloudinaryBackend()
    if name == "local":
        from .local import LocalBackend

        return LocalBackend()
    raise ValueError(
        f"Unknown STORAGE_BACKEND={name!r}. Expected one of: r2, cloudinary, local."
    )


def _selected_name() -> str:
    return (os.getenv("STORAGE_BACKEND") or _DEFAULT_BACKEND).strip().lower()


def get_storage(name: str | None = None) -> StorageBackend:
    """Return the storage backend singleton (per backend name).

    Reads ``STORAGE_BACKEND`` when ``name`` is omitted. Instances are cached so a
    boto3 client is built at most once per process.
    """
    key = (name or _selected_name())
    inst = _instances.get(key)
    if inst is None:
        with _lock:
            inst = _instances.get(key)
            if inst is None:
                inst = _build(key)
                if not inst.available():
                    logger.warning(
                        "Storage backend %r selected but not fully configured; "
                        "uploads through it will fail until env is set.",
                        key,
                    )
                _instances[key] = inst
    return inst


def storage_available() -> bool:
    """True if the *active* backend has the config it needs to upload.

    Replaces the old ``cloudinary_credentials_configured()`` gate: callers use it
    to decide between remote upload and a local-disk fallback URL.
    """
    try:
        return get_storage().available()
    except Exception:  # noqa: BLE001
        return False


def create_presigned_upload(
    *, key: str, content_type: str, expires_in: int = 15 * 60
) -> PresignedUpload | None:
    """Return a direct-upload target when the selected backend supports one."""
    backend = get_storage()
    creator = getattr(backend, "create_presigned_upload", None)
    if creator is None:
        return None
    return creator(key=key, content_type=content_type, expires_in=expires_in)


def multipart_supported() -> bool:
    """Whether the active backend can run a resumable multipart upload.

    The frontend probes this (via the create endpoint's 501) and falls back to
    the legacy single-request path, so a Cloudinary- or local-disk-configured
    install keeps working exactly as before.
    """
    try:
        backend = get_storage()
        return backend.available() and hasattr(backend, "create_multipart_upload")
    except Exception:  # noqa: BLE001
        return False


def reset_storage_cache() -> None:
    """Testing helper — drop cached instances so env changes take effect."""
    with _lock:
        _instances.clear()


__all__ = [
    "StorageBackend",
    "UploadResult",
    "PresignedUpload",
    "build_key",
    "guess_content_type",
    "get_storage",
    "storage_available",
    "create_presigned_upload",
    "multipart_supported",
    "reset_storage_cache",
]
