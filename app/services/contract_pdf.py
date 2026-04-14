"""Build a simple signed-contract PDF (ReportLab)."""

from __future__ import annotations

import base64
import io
import logging
import re
from datetime import datetime

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image as RLImage
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

logger = logging.getLogger(__name__)


def _xml_escape(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _paragraphs_from_body(body: str, styles) -> list:
    parts: list = []
    for line in (body or "").split("\n"):
        parts.append(Paragraph(_xml_escape(line) or " ", styles["Normal"]))
        parts.append(Spacer(1, 4))
    return parts


_DATA_URL = re.compile(r"^data:image/(png|jpeg|jpg);base64,(.+)$", re.I | re.S)


def build_signed_contract_pdf(
    title: str,
    body: str,
    signer_name: str,
    signer_email: str,
    signature_data: str,
    signed_at: datetime,
) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        rightMargin=54,
        leftMargin=54,
        topMargin=54,
        bottomMargin=54,
    )
    styles = getSampleStyleSheet()
    story: list = []

    story.append(Paragraph("<b>Signed agreement</b>", styles["Title"]))
    story.append(Spacer(1, 12))
    story.append(Paragraph(_xml_escape(title), styles["Heading2"]))
    story.append(Spacer(1, 12))
    story.extend(_paragraphs_from_body(body, styles))

    story.append(Spacer(1, 18))
    story.append(Paragraph("<b>Signer</b>", styles["Heading2"]))
    story.append(Paragraph(_xml_escape(f"Name: {signer_name}"), styles["Normal"]))
    story.append(Paragraph(_xml_escape(f"Email: {signer_email}"), styles["Normal"]))
    story.append(
        Paragraph(_xml_escape(f"Signed at (UTC): {signed_at.isoformat()}"), styles["Normal"])
    )
    story.append(Spacer(1, 12))
    story.append(Paragraph("<b>Signature</b>", styles["Heading2"]))

    m = _DATA_URL.match((signature_data or "").strip())
    if m:
        try:
            raw = base64.b64decode(m.group(2).strip())
            img = RLImage(ImageReader(io.BytesIO(raw)), width=220, height=80)
            story.append(img)
        except Exception:
            logger.exception("Could not embed signature image; falling back to text")
            story.append(Paragraph(_xml_escape(signature_data), styles["Normal"]))
    else:
        story.append(Paragraph(_xml_escape(signature_data), styles["Normal"]))

    doc.build(story)
    out = buf.getvalue()
    buf.close()
    return out
