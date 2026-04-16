from __future__ import annotations

import os

from app.db.database import SessionLocal
from app.db.models import ReviewForensicAsset, ReviewLink
from app.services.review_media import build_review_media_url


def package_forensic_asset_job(forensic_asset_id: int) -> bool:
    db = SessionLocal()
    try:
        asset = (
            db.query(ReviewForensicAsset)
            .filter(ReviewForensicAsset.id == forensic_asset_id)
            .first()
        )
        if not asset:
            return False
        link = db.query(ReviewLink).filter(ReviewLink.id == asset.review_link_id).first()
        if not link:
            asset.package_status = "failed"
            db.commit()
            return False
        api_base = f"{os.getenv('BACKEND_BASE_URL', 'http://localhost:8000').rstrip('/')}/api"
        playback_url = build_review_media_url(
            api_base=api_base,
            token=link.token,
            session_id=asset.review_session_id,
            purpose="playback",
        )
        asset.playback_manifest_url = playback_url
        asset.package_status = "ready"
        meta = dict(asset.package_metadata or {})
        meta["packaged_by"] = "review_forensic_job"
        asset.package_metadata = meta
        db.commit()
        return True
    finally:
        db.close()
