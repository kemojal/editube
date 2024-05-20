import os
from fastapi import UploadFile

UPLOAD_DIRECTORY = "./uploads"

def upload_file(file: UploadFile):
    try:
        os.makedirs(UPLOAD_DIRECTORY, exist_ok=True)
        file_path = os.path.join(UPLOAD_DIRECTORY, file.filename)
        with open(file_path, "wb") as buffer:
            buffer.write(file.file.read())
        return file_path
    except Exception as e:
        raise e

def delete_file(file_path: str):
    try:
        os.remove(file_path)
    except Exception as e:
        raise e