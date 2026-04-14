import cloudinary
import logging
import os
import re

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import api_router

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Editube API",
    description=(
        "Backend API for Editube, powering authentication, projects, uploads, "
        "video collaboration, comments, notifications, and analytics."
    ),
    version="1.0.0",
    contact={
        "name": "Editube Team",
    },
)


# Explicit production / known hosts; local dev also covered by allow_origin_regex below.
origins = [
    "http://localhost:3000",
    "http://localhost:3001",
    "http://localhost:3002",
    "http://localhost:3003",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:3001",
    "http://127.0.0.1:3002",
    "http://127.0.0.1:3003",
    "https://editube-kemojals-projects.vercel.app",
]
_extra = os.getenv("CORS_ORIGINS", "")
if _extra.strip():
    origins.extend(o.strip() for o in _extra.split(",") if o.strip())

# Browsers may use 127.0.0.1 or [::1] even when the bar says "localhost"; any dev port.
_local_origin_regex = r"^http://(localhost|127\.0\.0\.1|\[::1\]):\d+$"
_local_origin_pattern = re.compile(_local_origin_regex)


def _cors_headers_for_request(request: Request) -> dict[str, str]:
    """Mirror CORSMiddleware so error responses still expose CORS (avoids misleading browser CORS noise on 500)."""
    origin = request.headers.get("origin")
    if not origin:
        return {}
    if origin not in origins and not _local_origin_pattern.fullmatch(origin):
        return {}
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Credentials": "true",
    }


app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_origin_regex=_local_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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


@app.get("/health")
async def health():
    """Liveness probe for reverse proxies and Dokploy."""
    return {"status": "ok"}


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
        headers=_cors_headers_for_request(request),
    )