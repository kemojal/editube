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

from .base import PresignedUpload, UploadResult

# Multipart threshold/chunk — mirror the 6 MB the Cloudinary path used so large
# videos stream up in parts instead of one giant request.
_MULTIPART_CHUNK = 6 * 1024 * 1024
_PRESIGN_TTL_SECONDS = 15 * 60


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

    def create_presigned_upload(
        self,
        *,
        key: str,
        content_type: str,
        expires_in: int = _PRESIGN_TTL_SECONDS,
    ) -> PresignedUpload:
        """Create a single-use-style PUT target so large browser uploads bypass API disk."""
        upload_url = self._s3().generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self._bucket,
                "Key": key,
                "ContentType": content_type,
            },
            ExpiresIn=expires_in,
        )
        return PresignedUpload(
            upload_url=str(upload_url),
            public_url=self.public_url(key),
            key=key,
            headers={"Content-Type": content_type},
        )

    # -- multipart (resumable) --------------------------------------------
    # S3 multipart is what makes browser uploads survivable: each part is its
    # own request with its own retry, a failure at 90% re-sends one part
    # instead of starting over, and the single-PUT 5 GB ceiling disappears.

    def create_multipart_upload(self, *, key: str, content_type: str) -> str:
        response = self._s3().create_multipart_upload(
            Bucket=self._bucket, Key=key, ContentType=content_type
        )
        return str(response["UploadId"])

    def presign_part_urls(
        self,
        *,
        key: str,
        upload_id: str,
        part_count: int,
        expires_in: int = 6 * 60 * 60,
    ) -> list[str]:
        """One presigned `upload_part` URL per part, 1-indexed.

        Presigning is local HMAC work — no network round trip per URL — so
        batching the whole set into the create response costs one request
        total. Six-hour expiry: a 50 GB upload on a slow hotel line is exactly
        the case this flow exists for.
        """
        s3 = self._s3()
        return [
            str(
                s3.generate_presigned_url(
                    "upload_part",
                    Params={
                        "Bucket": self._bucket,
                        "Key": key,
                        "UploadId": upload_id,
                        "PartNumber": part_number,
                    },
                    ExpiresIn=expires_in,
                )
            )
            for part_number in range(1, part_count + 1)
        ]

    def complete_multipart_upload(
        self, *, key: str, upload_id: str, parts: list[dict]
    ) -> str:
        """`parts` = [{"part_number": n, "etag": "..."}, ...]. Returns the public URL."""
        self._s3().complete_multipart_upload(
            Bucket=self._bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={
                "Parts": [
                    {"PartNumber": int(p["part_number"]), "ETag": str(p["etag"])}
                    for p in sorted(parts, key=lambda p: int(p["part_number"]))
                ]
            },
        )
        return self.public_url(key)

    def abort_multipart_upload(self, *, key: str, upload_id: str) -> None:
        """Frees the already-uploaded parts, which otherwise bill forever."""
        self._s3().abort_multipart_upload(
            Bucket=self._bucket, Key=key, UploadId=upload_id
        )

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
