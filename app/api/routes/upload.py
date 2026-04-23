from fastapi import APIRouter, HTTPException, status, UploadFile, File
from app.utils.cloudinary import upload_file_to_cloudinary_with_meta

router = APIRouter(prefix="/upload", tags=["Upload"])

@router.post("/video")
async def handle_upload(video_file: UploadFile = File(...)):
    try:
        upload = upload_file_to_cloudinary_with_meta(video_file, resource_type="video")
        return {"file_path": str(upload["url"])}
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))