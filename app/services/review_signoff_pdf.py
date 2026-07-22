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
    from app.storage import build_key, get_storage, storage_available

    if not storage_available():
        logger.warning("Storage backend not available; review sign-off PDF not uploaded")
        return None
    try:
        folder = os.environ.get("CLOUDINARY_REVIEW_SIGNOFFS_FOLDER", "review_signoffs")
        key = build_key(
            folder=folder,
            public_id=f"review_signoff_{signoff_id}.pdf",
            content_type="application/pdf",
        )
        return get_storage().upload_bytes(
            pdf_bytes, key=key, content_type="application/pdf"
        ).url
    except Exception:
        logger.exception("Review sign-off PDF upload failed")
        return None
