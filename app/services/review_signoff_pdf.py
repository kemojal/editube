"""Generate and optionally upload review sign-off PDFs (ReportLab)."""

from __future__ import annotations

import io
import logging
import os
from typing import Optional

from app.services.contract_pdf import build_signed_contract_pdf

logger = logging.getLogger(__name__)


def build_review_signoff_pdf_bytes(
    declaration_text: str,
    signer_name: str,
    signer_email: str,
    signature_payload: str,
    signed_at,
) -> bytes:
    """signature_payload: drawn image data URL or typed name line."""
    return build_signed_contract_pdf(
        "Video review sign-off",
        declaration_text,
        signer_name or "—",
        signer_email or "—",
        signature_payload or "—",
        signed_at,
    )


def upload_review_signoff_pdf(pdf_bytes: bytes, signoff_id: int) -> Optional[str]:
    try:
        import app.utils.cloudinary  # noqa: F401
        import cloudinary.uploader
    except Exception:
        logger.warning("Cloudinary not available; review sign-off PDF not uploaded")
        return None
    try:
        folder = os.environ.get("CLOUDINARY_REVIEW_SIGNOFFS_FOLDER", "review_signoffs")
        r = cloudinary.uploader.upload(
            io.BytesIO(pdf_bytes),
            resource_type="raw",
            folder=folder,
            public_id=f"review_signoff_{signoff_id}",
            format="pdf",
            overwrite=True,
        )
        return r.get("secure_url")
    except Exception:
        logger.exception("Review sign-off PDF upload failed")
        return None
