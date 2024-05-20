import cloudinary.uploader

def upload_file_to_cloudinary(file):
    try:
        # Upload the file to Cloudinary
        result = cloudinary.uploader.upload(file.file, resource_type="video")
        # Return the URL of the uploaded video
        return result.get("secure_url")
    except Exception as e:
        raise e
