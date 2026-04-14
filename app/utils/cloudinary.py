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


def upload_file_to_cloudinary(file: UploadFile, resource_type: str = "video") -> str:
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
        return url
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
