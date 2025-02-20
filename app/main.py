import cloudinary
import os 
from dotenv import load_dotenv
from fastapi import FastAPI, Request
# from app.websocket_manager import app as websocket_app
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import logging


from .api import api_router


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()


origins = [
   
    "http://localhost:3000",
    "http://localhost:3001",
    "http://localhost:3002",
    "http://localhost:3003",
    "https://editube-kemojals-projects.vercel.app",

]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)



load_dotenv()
          
# cloudinary.config( 
#   cloud_name = "dtpnbesbx", 
#   api_key = "811133693665998", 
#   api_secret = "1YJOBmJ9LN1Aqhyc8AlUoAOHF9A" 
# )
cloudinary.config( 
  cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME"), 
  api_key = os.getenv("CLOUDINARY_API_KEY"), 
  api_secret = os.getenv("CLOUDINARY_API_SECRET") 
)

app.include_router(api_router)

# Include the WebSocket app
# app.mount("/", websocket_app)

@app.get("/")
async def read_item():
    return {"hello word"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)





@app.exception_handler(Exception)
async def unicorn_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled error: {exc}")
    return JSONResponse(
        status_code=500,
        content={"message": "Internal server error"},
    )