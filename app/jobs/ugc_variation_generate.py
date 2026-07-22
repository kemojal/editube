"""RQ job: fan out N variations for a campaign (optional async path).

The HTTP route builds variations inline (fast, no AI calls), but this wrapper
lets the engine run in the background for very large batches.
"""

from __future__ import annotations

import logging
from typing import Any

from app.db.database import SessionLocal
from app.db.models import UgcCampaign
from app.services.ugc_variation_engine import build_variations

logger = logging.getLogger(__name__)


def ugc_variation_generate_job(campaign_id: int, count: int, dimensions: dict[str, Any] | None = None) -> None:
    db = SessionLocal()
    try:
        campaign = db.query(UgcCampaign).filter(UgcCampaign.id == campaign_id).first()
        if campaign is None:
            logger.error("ugc_variation_generate_job: campaign %s not found", campaign_id)
            return
        created = build_variations(db, campaign, count, dimensions)
        logger.info("ugc_variation_generate_job: campaign %s created %d variations", campaign_id, len(created))
    except Exception:  # noqa: BLE001
        logger.exception("ugc_variation_generate_job failed for campaign %s", campaign_id)
        db.rollback()
    finally:
        db.close()
