"""Cloudinary backend — legacy / rollback path.

Reproduces the behavior the app had before R2: chunked ``upload_large`` for video,
regular upload for images, ``resource_type=raw`` for zips/PDFs. Selected with
``STORAGE_BACKEND=cloudinary`` (the safe default until R2 is verified).
"""
from __future__ import annotations

import io
import os
from pathlib import Path
from typing import BinaryIO

from .base import UploadResult

_VIDEO_CHUNK_BYTES = 6 * 1024 * 1024
_UPLOAD_TIMEOUT = 900


def _resource_type(content_type: str) -> str:
    if content_type.startswith("image/"):
        return "image"
    if content_type.startswith("video/"):
        return "video"
    return "raw"


def _split_key(key: str) -> tuple[str, str]:
    """key ``folder/sub/name.ext`` -> (``folder/sub``, ``name``) for Cloudinary."""
    p = Path(key)
    folder = str(p.parent) if str(p.parent) not in (".", "") else ""
    public_id = p.stem  # Cloudinary manages the extension itself
    return folder, public_id


class CloudinaryBackend:
    name = "cloudinary"

    def __init__(self) -> None:
        self._configured = False

    def available(self) -> bool:
        return bool(
            os.getenv("CLOUDINARY_CLOUD_NAME")
            and os.getenv("CLOUDINARY_API_KEY")
            and os.getenv("CLOUDINARY_API_SECRET")
        )

    def _ensure_config(self):
        import cloudinary

        if not self._configured:
            cloudinary.config(
                cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
                api_key=os.getenv("CLOUDINARY_API_KEY"),
                api_secret=os.getenv("CLOUDINARY_API_SECRET"),
            )
            self._configured = True

    def _upload(self, source, *, key: str, content_type: str, is_path: bool) -> UploadResult:
        import cloudinary.uploader

        self._ensure_config()
        resource_type = _resource_type(content_type)
        folder, public_id = _split_key(key)
        opts: dict = {
            "resource_type": resource_type,
            "public_id": public_id,
            "timeout": _UPLOAD_TIMEOUT,
        }
        if folder:
            opts["folder"] = folder

        large = resource_type == "video"
        if is_path and large and Path(source).stat().st_size > _VIDEO_CHUNK_BYTES:
            result = cloudinary.uploader.upload_large(str(source), chunk_size=_VIDEO_CHUNK_BYTES, **opts)
        elif not is_path and large:
            result = cloudinary.uploader.upload_large(source, chunk_size=_VIDEO_CHUNK_BYTES, **opts)
        else:
            result = cloudinary.uploader.upload(str(source) if is_path else source, **opts)

        url = result.get("secure_url")
        if not url:
            raise RuntimeError("Cloudinary returned no secure_url")
        return UploadResult(
            url=str(url),
            bytes=int(result.get("bytes") or 0),
            key=str(result.get("public_id") or public_id),
            content_type=content_type,
        )

    def upload_stream(self, fileobj: BinaryIO, *, key: str, content_type: str) -> UploadResult:
        if hasattr(fileobj, "seek"):
            try:
                fileobj.seek(0)
            except (OSError, ValueError):
                pass
        return self._upload(fileobj, key=key, content_type=content_type, is_path=False)

    def upload_path(self, path: str | Path, *, key: str, content_type: str) -> UploadResult:
        p = Path(path).resolve()
        if not p.is_file():
            raise FileNotFoundError(str(p))
        return self._upload(p, key=key, content_type=content_type, is_path=True)

    def upload_bytes(self, data: bytes, *, key: str, content_type: str) -> UploadResult:
        return self.upload_stream(io.BytesIO(data), key=key, content_type=content_type)

    def public_url(self, key: str) -> str:
        # Cloudinary URLs come back from upload; there's no deterministic base URL
        # to reconstruct. Callers in cloudinary mode use UploadResult.url instead.
        raise NotImplementedError("CloudinaryBackend has no static public_url; use UploadResult.url")
