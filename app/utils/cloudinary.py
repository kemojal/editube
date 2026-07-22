"""Storage facade (historically Cloudinary-only).

These functions keep their original names/signatures so existing call sites keep
working, but they now dispatch to the pluggable backend selected by
``STORAGE_BACKEND`` (see ``app/storage`` and ``docs/r2-storage-migration-plan.md``).
Cloudinary is one backend among r2/local; nothing here talks to the Cloudinary SDK
directly anymore.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import HTTPException, UploadFile, status

from app.storage import build_key, get_storage, guess_content_type, storage_available

# Default folders (object-key prefixes) for the untagged upload paths. Overridable
# so operators can organize the bucket; new uploads only.
_VIDEO_FOLDER = os.getenv("STORAGE_VIDEO_FOLDER", "videos")
_IMAGE_FOLDER = os.getenv("STORAGE_IMAGE_FOLDER", "images")


def _too_large(err: str) -> bool:
    e = err.lower()
    return "413" in e or "entitytoolarge" in e or "too large" in e


def _folder_for(resource_type: str) -> str:
    return _IMAGE_FOLDER if resource_type == "image" else _VIDEO_FOLDER


def upload_file_to_cloudinary_with_meta(file: UploadFile, resource_type: str = "video") -> dict:
    """Upload an ``UploadFile`` (video or image). Returns ``{url, bytes, public_id, resource_type}``."""
    content_type = file.content_type or guess_content_type(file.filename, resource_type=resource_type)
    key = build_key(
        folder=_folder_for(resource_type),
        filename=file.filename,
        content_type=content_type,
    )
    try:
        result = get_storage().upload_stream(file.file, key=key, content_type=content_type)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        err = str(e)
        if _too_large(err):
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="The upload was rejected because the file is too large.",
            ) from e
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Storage upload failed: {err}",
        ) from e
    if not result.url:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Storage returned no URL for the upload.",
        )
    return {
        "url": result.url,
        "bytes": result.bytes,
        "public_id": result.key,
        "resource_type": resource_type,
    }


def upload_file_to_cloudinary(file: UploadFile, resource_type: str = "video") -> str:
    return str(upload_file_to_cloudinary_with_meta(file, resource_type=resource_type)["url"])


def cloudinary_credentials_configured() -> bool:
    """Whether the active storage backend can accept uploads.

    Name kept for backwards-compatibility with existing gate call sites; it now
    reflects the selected ``STORAGE_BACKEND``, not Cloudinary specifically.
    """
    return storage_available()


def upload_local_path_to_cloudinary(
    file_path: str | Path,
    *,
    resource_type: str,
    folder: str,
    public_id: str,
) -> str:
    """Upload a file already on disk (e.g. ffmpeg output). Returns the public URL."""
    p = Path(file_path).resolve()
    if not p.is_file():
        raise FileNotFoundError(str(p))
    content_type = guess_content_type(p.name, resource_type=resource_type)
    key = build_key(folder=folder, public_id=public_id, filename=p.name, content_type=content_type)
    try:
        result = get_storage().upload_path(p, key=key, content_type=content_type)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Storage upload failed: {e}") from e
    if not result.url:
        raise RuntimeError("Storage returned no URL")
    return str(result.url)


def upload_image_bytes(
    data: bytes,
    *,
    mime_type: str = "image/png",
    folder: str = "broll",
    public_id: str = "img",
) -> str:
    """Upload raw image bytes (e.g. from Gemini). Returns the public URL."""
    key = build_key(folder=folder, public_id=public_id, content_type=mime_type)
    try:
        result = get_storage().upload_bytes(data, key=key, content_type=mime_type)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Storage upload failed: {e}",
        ) from e
    if not result.url:
        raise RuntimeError("Storage returned no URL")
    return str(result.url)


async def upload_image(image: UploadFile):
    """Upload an image ``UploadFile``. Returns the public URL."""
    content_type = image.content_type or guess_content_type(image.filename, resource_type="image")
    key = build_key(folder=_IMAGE_FOLDER, filename=image.filename, content_type=content_type)
    try:
        result = get_storage().upload_stream(image.file, key=key, content_type=content_type)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        err = str(e)
        if _too_large(err):
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Image/video file is too large.",
            ) from e
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error uploading image: {err}",
        ) from e
    return result.url
