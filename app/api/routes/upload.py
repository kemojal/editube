import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
# The multipart parser builds `starlette.datastructures.UploadFile`; FastAPI's
# `UploadFile` is a subclass of it, so isinstance against the subclass fails.
from starlette.datastructures import UploadFile

from app.db.models import User
from app.storage import (
    build_key,
    create_presigned_upload,
    get_storage,
    guess_content_type,
    multipart_supported,
)
from app.utils.cloudinary import upload_file_to_cloudinary_with_meta
from app.utils.security import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/upload", tags=["Upload"])

#: Field name the frontend sends (see `lib/api/upload.ts::buildVideoUploadFormData`).
VIDEO_FIELD = "video_file"

# Cloudflare R2's single PUT limit. Larger uploads need a multipart-direct flow,
# not a fallback through this API (which would recreate the disk-space failure).
MAX_DIRECT_UPLOAD_BYTES = 5 * 1024**3


class DirectUploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=512)
    content_type: str | None = Field(default=None, max_length=255)
    size: int = Field(gt=0)


class DirectUploadResponse(BaseModel):
    upload_url: str
    file_path: str
    headers: dict[str, str]


@router.post("/video/presign", response_model=DirectUploadResponse)
def create_video_upload(
    body: DirectUploadRequest,
    _current_user: User = Depends(get_current_user),
):
    """Create a browser-to-object-storage upload target without spooling locally."""
    if body.size > MAX_DIRECT_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Videos larger than 5 GB require multipart upload.",
        )

    content_type = body.content_type or guess_content_type(
        body.filename, resource_type="video"
    )
    if not (content_type.startswith("video/") or content_type == "application/octet-stream"):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="The selected file is not a supported video.",
        )

    key = build_key(folder="videos", filename=body.filename, content_type=content_type)
    try:
        target = create_presigned_upload(key=key, content_type=content_type)
    except Exception as e:  # noqa: BLE001
        logger.exception("Could not create direct upload target")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not prepare video upload: {e}",
        ) from e
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Direct uploads are not supported by the active storage backend.",
        )
    return DirectUploadResponse(
        upload_url=target.upload_url,
        file_path=target.public_url,
        headers=target.headers,
    )


@router.post("/video")
async def handle_upload(request: Request):
    # The form is parsed here rather than via `video_file: UploadFile = File(...)`
    # so parse failures keep their cause. FastAPI wraps anything raised out of
    # `request.form()` in a bare 400 "There was an error parsing the body", which
    # hides the actual fault — a full disk while spooling the part, a client that
    # went away mid-upload, a malformed boundary — and made this endpoint
    # undebuggable from the logs alone.
    try:
        form = await request.form()
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to parse multipart body for POST /upload/video")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not read the uploaded file ({type(e).__name__}: {e}).",
        ) from e

    video_file = form.get(VIDEO_FIELD)
    if not isinstance(video_file, UploadFile):
        got = "nothing" if video_file is None else "a text field"
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Expected a file in the '{VIDEO_FIELD}' field, got {got}.",
        )

    try:
        upload = upload_file_to_cloudinary_with_meta(video_file, resource_type="video")
    except HTTPException:
        # The storage helper already maps its own failures (413 too large,
        # 502 backend refused). Re-raising as a 500 threw that detail away.
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("Storage upload failed for POST /upload/video")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        ) from e

    return {"file_path": str(upload["url"])}


# --- Resumable multipart upload ---------------------------------------------
# The single-PUT presign above dies at 5 GB (an R2 hard limit) and restarts
# from zero on any network hiccup — "upload fails at 90% and starts over" was
# the audit's canonical wound. Multipart fixes both: each part retries alone,
# and the ceiling moves to the terabyte range.

MULTIPART_PART_SIZE = 32 * 1024 * 1024  # S3 minimum is 5 MB; 32 keeps part counts sane
MAX_MULTIPART_UPLOAD_BYTES = 100 * 1024**3


class MultipartCreateRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=512)
    content_type: str | None = Field(default=None, max_length=255)
    size: int = Field(gt=0)


class MultipartCreateResponse(BaseModel):
    key: str
    upload_id: str
    part_size: int
    part_count: int
    part_urls: list[str]
    #: Where the file will live once completed — hand this to /from-upload.
    file_path: str


class MultipartPart(BaseModel):
    part_number: int = Field(ge=1)
    etag: str = Field(min_length=1)


class MultipartCompleteRequest(BaseModel):
    key: str
    upload_id: str
    parts: list[MultipartPart] = Field(min_length=1)


class MultipartAbortRequest(BaseModel):
    key: str
    upload_id: str


@router.post("/multipart/create", response_model=MultipartCreateResponse)
def create_multipart_video_upload(
    body: MultipartCreateRequest,
    _current_user: User = Depends(get_current_user),
):
    if not multipart_supported():
        # 501, not 500: the client probes this and falls back to the legacy
        # single-request path on Cloudinary/local installs.
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Resumable upload needs S3-compatible storage (R2).",
        )
    if body.size > MAX_MULTIPART_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Uploads are capped at 100 GB.",
        )

    content_type = body.content_type or guess_content_type(
        body.filename, resource_type="video"
    )
    if not (content_type.startswith("video/") or content_type == "application/octet-stream"):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="The selected file is not a supported video.",
        )

    key = build_key(folder="videos", filename=body.filename, content_type=content_type)
    part_count = max(1, -(-body.size // MULTIPART_PART_SIZE))  # ceil

    backend = get_storage()
    try:
        upload_id = backend.create_multipart_upload(key=key, content_type=content_type)
        part_urls = backend.presign_part_urls(
            key=key, upload_id=upload_id, part_count=part_count
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Multipart create failed for %s", body.filename)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The storage backend refused the upload.",
        ) from e

    return MultipartCreateResponse(
        key=key,
        upload_id=upload_id,
        part_size=MULTIPART_PART_SIZE,
        part_count=part_count,
        part_urls=part_urls,
        file_path=backend.public_url(key),
    )


@router.post("/multipart/complete")
def complete_multipart_video_upload(
    body: MultipartCompleteRequest,
    _current_user: User = Depends(get_current_user),
):
    if not multipart_supported():
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Unsupported")
    try:
        file_path = get_storage().complete_multipart_upload(
            key=body.key,
            upload_id=body.upload_id,
            parts=[p.model_dump() for p in body.parts],
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Multipart complete failed for %s", body.key)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The storage backend could not assemble the upload.",
        ) from e
    return {"file_path": file_path}


@router.post("/multipart/abort")
def abort_multipart_video_upload(
    body: MultipartAbortRequest,
    _current_user: User = Depends(get_current_user),
):
    """Cancel and free the parts already stored — abandoned parts bill forever."""
    if not multipart_supported():
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Unsupported")
    try:
        get_storage().abort_multipart_upload(key=body.key, upload_id=body.upload_id)
    except Exception:  # noqa: BLE001 — best-effort cleanup; nothing to surface
        logger.exception("Multipart abort failed for %s", body.key)
    return {"ok": True}
