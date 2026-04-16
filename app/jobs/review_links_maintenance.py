from __future__ import annotations

from datetime import datetime, timezone

from app.db.database import SessionLocal
from app.db.models import ReviewLink
from app.services.security_audit import log_security_audit_event


def auto_revoke_expired_review_links() -> int:
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        links = (
            db.query(ReviewLink)
            .filter(
                ReviewLink.expires_at.isnot(None),
                ReviewLink.expires_at <= now,
                ReviewLink.revoked_at.is_(None),
            )
            .all()
        )
        for link in links:
            link.revoked_at = now
            link.revocation_reason = "expired_auto_revoke"
            log_security_audit_event(
                db,
                action="review_link.auto_revoked",
                resource_type="review_link",
                resource_id=str(link.id),
                actor_type="system",
                review_link_id=link.id,
                video_id=link.video_id,
                metadata={"expires_at": link.expires_at.isoformat() if link.expires_at else None},
            )
        db.commit()
        return len(links)
    finally:
        db.close()
