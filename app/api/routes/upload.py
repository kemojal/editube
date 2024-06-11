# router/upload.py

from fastapi import APIRouter, HTTPException, status, UploadFile
from typing import List
from app.utils.cloudinary import upload_image

from pydantic import BaseModel

router = APIRouter(
    prefix="/upload",
    tags=["Upload"],
)


class Item(BaseModel):
    image: str
    

    class Config:
        orm_mode = True

@router.post("/video")
async def handle_upload(image: Item):
    print("image test 00 ", image.filename)
    try:
        url = await upload_image(image)
        return {
            "data": {
                "url": url
            }
        }
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=e)