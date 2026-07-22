"""Cloudflare R2 backend (S3-compatible, via boto3).

Public reads are served from ``R2_PUBLIC_BASE_URL`` (an r2.dev dev URL or a bound
custom domain) — see ``editube/R2_STORAGE_SETUP.md``. The S3 endpoint is derived
from the account id unless ``R2_ENDPOINT_URL`` is set explicitly.
"""
from __future__ import annotations

import io
import os
from pathlib import Path
from typing import BinaryIO

from .base import UploadResult

# Multipart threshold/chunk — mirror the 6 MB the Cloudinary path used so large
# videos stream up in parts instead of one giant request.
_MULTIPART_CHUNK = 6 * 1024 * 1024


class R2Backend:
    name = "r2"

    def __init__(self) -> None:
        self._account_id = os.getenv("R2_ACCOUNT_ID")
        self._access_key = os.getenv("R2_ACCESS_KEY_ID")
        self._secret_key = os.getenv("R2_SECRET_ACCESS_KEY")
        self._bucket = os.getenv("R2_BUCKET")
        self._public_base = (os.getenv("R2_PUBLIC_BASE_URL") or "").rstrip("/")
        self._endpoint = os.getenv("R2_ENDPOINT_URL") or (
            f"https://{self._account_id}.r2.cloudflarestorage.com"
            if self._account_id
            else None
        )
        self._client = None  # lazy — don't build a boto3 client unless used

    # -- config -----------------------------------------------------------
    def available(self) -> bool:
        return bool(
            self._account_id
            and self._access_key
            and self._secret_key
            and self._bucket
            and self._public_base
        )

    def _s3(self):
        if self._client is None:
            import boto3
            from boto3.s3.transfer import TransferConfig  # noqa: F401 (imported for side-effect parity)
            from botocore.config import Config

            if not self.available():
                raise RuntimeError(
                    "R2 backend is not configured. Required env: R2_ACCOUNT_ID, "
                    "R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_PUBLIC_BASE_URL."
                )
            self._client = boto3.client(
                "s3",
                endpoint_url=self._endpoint,
                aws_access_key_id=self._access_key,
                aws_secret_access_key=self._secret_key,
                region_name="auto",
                config=Config(s3={"addressing_style": "path"}),
            )
        return self._client

    def _transfer_config(self):
        from boto3.s3.transfer import TransferConfig

        return TransferConfig(
            multipart_threshold=_MULTIPART_CHUNK,
            multipart_chunksize=_MULTIPART_CHUNK,
        )

    # -- writes -----------------------------------------------------------
    def upload_stream(self, fileobj: BinaryIO, *, key: str, content_type: str) -> UploadResult:
        if hasattr(fileobj, "seek"):
            try:
                fileobj.seek(0)
            except (OSError, ValueError):
                pass
        s3 = self._s3()
        s3.upload_fileobj(
            fileobj,
            self._bucket,
            key,
            ExtraArgs={"ContentType": content_type},
            Config=self._transfer_config(),
        )
        return self._result(key, content_type)

    def upload_path(self, path: str | Path, *, key: str, content_type: str) -> UploadResult:
        p = Path(path).resolve()
        if not p.is_file():
            raise FileNotFoundError(str(p))
        s3 = self._s3()
        s3.upload_file(
            str(p),
            self._bucket,
            key,
            ExtraArgs={"ContentType": content_type},
            Config=self._transfer_config(),
        )
        return self._result(key, content_type, known_bytes=p.stat().st_size)

    def upload_bytes(self, data: bytes, *, key: str, content_type: str) -> UploadResult:
        return self.upload_stream(io.BytesIO(data), key=key, content_type=content_type)

    # -- reads ------------------------------------------------------------
    def public_url(self, key: str) -> str:
        return f"{self._public_base}/{key.lstrip('/')}"

    def _result(self, key: str, content_type: str, *, known_bytes: int | None = None) -> UploadResult:
        size = known_bytes
        if size is None:
            try:
                size = int(self._s3().head_object(Bucket=self._bucket, Key=key).get("ContentLength", 0))
            except Exception:  # noqa: BLE001 — size is best-effort metadata
                size = 0
        return UploadResult(
            url=self.public_url(key),
            bytes=int(size or 0),
            key=key,
            content_type=content_type,
        )
