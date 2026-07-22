"""Local-disk backend — self-hosted / dev / offline fallback.

Writes under ``./uploads`` (mounted at ``/uploads`` by ``app.main``) and serves
public URLs as ``<BASE_URL>/uploads/<key>``.
"""
from __future__ import annotations

import io
import os
import shutil
from pathlib import Path
from typing import BinaryIO

from .base import UploadResult

_UPLOAD_ROOT = Path(os.getenv("LOCAL_STORAGE_DIR", "./uploads")).resolve()


class LocalBackend:
    name = "local"

    def __init__(self) -> None:
        self._base_url = (os.getenv("BASE_URL") or "http://127.0.0.1:8000").rstrip("/")
        self._root = _UPLOAD_ROOT

    def available(self) -> bool:
        return True

    def _dest(self, key: str) -> Path:
        # Prevent path traversal escaping the upload root.
        dest = (self._root / key.lstrip("/")).resolve()
        if not str(dest).startswith(str(self._root)):
            raise ValueError(f"Refusing to write outside upload root: {key}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        return dest

    def upload_stream(self, fileobj: BinaryIO, *, key: str, content_type: str) -> UploadResult:
        if hasattr(fileobj, "seek"):
            try:
                fileobj.seek(0)
            except (OSError, ValueError):
                pass
        dest = self._dest(key)
        with open(dest, "wb") as out:
            shutil.copyfileobj(fileobj, out)
        return self._result(key, content_type, dest)

    def upload_path(self, path: str | Path, *, key: str, content_type: str) -> UploadResult:
        src = Path(path).resolve()
        if not src.is_file():
            raise FileNotFoundError(str(src))
        dest = self._dest(key)
        if src != dest:
            shutil.copy2(src, dest)
        return self._result(key, content_type, dest)

    def upload_bytes(self, data: bytes, *, key: str, content_type: str) -> UploadResult:
        return self.upload_stream(io.BytesIO(data), key=key, content_type=content_type)

    def public_url(self, key: str) -> str:
        return f"{self._base_url}/uploads/{key.lstrip('/')}"

    def _result(self, key: str, content_type: str, dest: Path) -> UploadResult:
        return UploadResult(
            url=self.public_url(key),
            bytes=dest.stat().st_size,
            key=key,
            content_type=content_type,
        )
