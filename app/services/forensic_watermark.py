from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.db.models import ReviewForensicAsset, ReviewLink, ReviewSession


def build_forensic_fingerprint(link: ReviewLink, session: ReviewSession, country_code: str | None) -> str:
    seed = "|".join(
        [
            str(link.id),
            str(session.id),
            (session.guest_email or "").lower(),
            session.ip_address or "",
            country_code or "",
            datetime.now(timezone.utc).strftime("%Y%m%d%H"),
            os.getenv("FORENSIC_WATERMARK_SALT", "editube-forensic-watermark"),
        ]
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def upsert_forensic_asset(
    db: Session,
    *,
    link: ReviewLink,
    session: ReviewSession,
    fingerprint: str,
    playback_manifest_url: str | None,
    ttl_minutes: int = 120,
) -> ReviewForensicAsset:
    row = (
        db.query(ReviewForensicAsset)
        .filter(
            ReviewForensicAsset.review_link_id == link.id,
            ReviewForensicAsset.review_session_id == session.id,
        )
        .first()
    )
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    if not row:
        row = ReviewForensicAsset(
            review_link_id=link.id,
            review_session_id=session.id,
            watermark_fingerprint=fingerprint,
            playback_manifest_url=playback_manifest_url,
            package_status="ready" if playback_manifest_url else "pending",
            expires_at=expires_at,
            package_metadata={"mode": link.watermark_mode},
        )
        db.add(row)
    else:
        row.watermark_fingerprint = fingerprint
        if playback_manifest_url:
            row.playback_manifest_url = playback_manifest_url
            row.package_status = "ready"
        row.expires_at = expires_at
        row.package_metadata = {"mode": link.watermark_mode}
    db.flush()
    return row
