import cloudinary.uploader
import cloudinary
import os
from cloudinary.uploader import upload
from fastapi import HTTPException, status, UploadFile
from dotenv import load_dotenv
def upload_file_to_cloudinary(file):
    try:
        # Upload the file to Cloudinary
        result = cloudinary.uploader.upload(file.file, resource_type="video")
        # Return the URL of the uploaded video
        return result.get("secure_url")
    except Exception as e:
        raise e





load_dotenv()

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET")
)

async def upload_image(image: UploadFile):
    print("image test ", image.filename)
    try:
        upload_result = upload(image.file, resource_type="video")
        file_url = upload_result['secure_url']
        return file_url
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Error uploading image: {e}")