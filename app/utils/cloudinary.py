import io
import os

import cloudinary
import cloudinary.uploader
from cloudinary.exceptions import Error as CloudinaryError
from dotenv import load_dotenv
from fastapi import HTTPException, UploadFile, status

load_dotenv()

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
)


# Smaller than Cloudinary’s default 20MB so strict nginx limits are less likely per chunk.
_VIDEO_CHUNK_BYTES = 6 * 1024 * 1024
_CLOUDINARY_UPLOAD_TIMEOUT = 900  # seconds; large videos + processing need headroom


def upload_file_to_cloudinary_with_meta(file: UploadFile, resource_type: str = "video") -> dict:
    try:
        stream = file.file
        if hasattr(stream, "seek"):
            stream.seek(0)
        if resource_type == "image":
            # Regular upload for avatars/images so jpeg/jpg/png/webp are handled correctly.
            result = cloudinary.uploader.upload(
                stream,
                resource_type="image",
                filename=file.filename or "avatar",
                timeout=_CLOUDINARY_UPLOAD_TIMEOUT,
                allowed_formats=["jpg", "jpeg", "png", "webp"],
            )
        else:
            # Chunked upload avoids nginx/Cloudinary 413 on large single-request bodies
            result = cloudinary.uploader.upload_large(
                stream,
                resource_type="video",
                filename=file.filename or "video",
                chunk_size=_VIDEO_CHUNK_BYTES,
                timeout=_CLOUDINARY_UPLOAD_TIMEOUT,
            )
        url = result.get("secure_url")
        if not url:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Cloudinary returned no URL for the upload.",
            )
        return {
            "url": url,
            "bytes": int(result.get("bytes") or 0),
            "public_id": result.get("public_id"),
            "resource_type": result.get("resource_type"),
        }
    except CloudinaryError as e:
        err = str(e)
        # Cloudinary/nginx returns HTML 413; SDK wraps it as Error parsing server response (413)
        if "413" in err:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(
                    "The upload was rejected because the file is too large for Cloudinary "
                    "(or exceeds the current plan limit). Try a smaller file or check "
                    "Cloudinary upload limits."
                ),
            ) from e
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Cloudinary upload failed: {err}",
        ) from e


def upload_file_to_cloudinary(file: UploadFile, resource_type: str = "video") -> str:
    result = upload_file_to_cloudinary_with_meta(file, resource_type=resource_type)
    return str(result["url"])


def cloudinary_credentials_configured() -> bool:
    return bool(
        os.getenv("CLOUDINARY_CLOUD_NAME")
        and os.getenv("CLOUDINARY_API_KEY")
        and os.getenv("CLOUDINARY_API_SECRET")
    )


def upload_local_path_to_cloudinary(
    file_path: str | Path,
    *,
    resource_type: str,
    folder: str,
    public_id: str,
) -> str:
    """
    Upload a file already on disk (e.g. ffmpeg output). Returns ``secure_url``.
    Uses chunked upload for large videos.
    """
    p = Path(file_path).resolve()
    if not p.is_file():
        raise FileNotFoundError(str(p))
    folder = folder.strip().strip("/")
    opts: dict = {
        "resource_type": resource_type,
        "folder": folder,
        "public_id": public_id.strip().strip("/"),
        "timeout": _CLOUDINARY_UPLOAD_TIMEOUT,
    }
    try:
        if resource_type == "video" and p.stat().st_size > _VIDEO_CHUNK_BYTES:
            result = cloudinary.uploader.upload_large(
                str(p),
                chunk_size=_VIDEO_CHUNK_BYTES,
                **opts,
            )
        else:
            result = cloudinary.uploader.upload(str(p), **opts)
    except CloudinaryError as e:
        raise RuntimeError(f"Cloudinary upload failed: {e}") from e
    url = result.get("secure_url")
    if not url:
        raise RuntimeError("Cloudinary returned no secure_url")
    return str(url)


def upload_image_bytes(
    data: bytes,
    *,
    mime_type: str = "image/png",
    folder: str = "broll",
    public_id: str = "img",
) -> str:
    """Upload raw image bytes (e.g. from Gemini) to Cloudinary. Returns ``secure_url``."""
    fmt = mime_type.split("/")[-1] if "/" in mime_type else "png"
    try:
        result = cloudinary.uploader.upload(
            io.BytesIO(data),
            resource_type="image",
            folder=folder,
            public_id=public_id,
            format=fmt,
        )
        url = result.get("secure_url")
        if not url:
            raise RuntimeError("Cloudinary returned no URL")
        return str(url)
    except CloudinaryError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Cloudinary upload failed: {e}",
        ) from e


async def upload_image(image: UploadFile):
    try:
        upload_result = cloudinary.uploader.upload(
            image.file,
            resource_type="image",
            allowed_formats=["jpg", "jpeg", "png", "webp"],
        )
        return upload_result["secure_url"]
    except CloudinaryError as e:
        err = str(e)
        if "413" in err:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Image/video file is too large for Cloudinary.",
            ) from e
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error uploading image: {err}",
        ) from e
