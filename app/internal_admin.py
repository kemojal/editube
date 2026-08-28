"""Dedicated internal-only API for privileged request-log investigation.

Deploy this as a separate service/process. The public API must not receive
LOG_READ_DATABASE_URL; this service must not receive LOG_WRITE_DATABASE_URL.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.request_logging.routes import router as request_logs_router
from app.request_logging.config import RequestLogSettings
from app.request_logging.crypto import PayloadCipher
from app.request_logging.database import dispose_log_engines


load_dotenv()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    settings = RequestLogSettings.from_env()
    if settings.write_database_url:
        raise RuntimeError(
            "Internal admin service must not receive LOG_WRITE_DATABASE_URL"
        )
    settings.validate_for_read()
    PayloadCipher(settings)
    yield
    dispose_log_engines()

app = FastAPI(
    title="Editube Internal Administration API",
    description="MFA-gated internal incident investigation endpoints.",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=_lifespan,
)

admin_origins = [
    origin.strip()
    for origin in (os.getenv("LOG_ADMIN_CORS_ORIGINS") or "").split(",")
    if origin.strip()
]
if admin_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=admin_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-Log-Step-Up-Token",
            "X-Log-Access-Reason",
        ],
    )

app.include_router(request_logs_router)


@app.get("/health", include_in_schema=False)
def health() -> dict[str, str]:
    return {"status": "ok", "service": "editube-internal-admin"}
