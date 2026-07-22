"""RQ job: render a UgcVariation to MP4. Refunds a credit on failure.

Mirrors app.jobs.clip_render. Run worker from editube/ with:
    rq worker -u "$REDIS_URL" default
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.db.models import UgcCampaign, UgcVariation
from app.services import ugc_credits
from app.services.ugc_render import render_variation

logger = logging.getLogger(__name__)


def _set_progress(db: Session, variation_id: int, progress: int, status: str | None = None) -> None:
    var = db.query(UgcVariation).filter(UgcVariation.id == variation_id).first()
    if var is None:
        return
    var.render_progress = max(0, min(100, int(progress)))
    if status:
        var.status = status
    db.commit()


def ugc_render_job(variation_id: int) -> None:
    db: Session = SessionLocal()
    try:
        var = db.query(UgcVariation).filter(UgcVariation.id == variation_id).first()
        if var is None:
            logger.error("ugc_render_job: variation %s not found", variation_id)
            return

        var.status = "rendering"
        var.render_progress = 0
        var.render_error = None
        db.commit()

        def on_progress(p: int) -> None:
            _set_progress(db, variation_id, p, status="rendering")

        render_variation(db, variation_id, on_progress=on_progress)

        var = db.query(UgcVariation).filter(UgcVariation.id == variation_id).first()
        if var is not None:
            var.status = "ready"
            var.render_progress = 100
            var.render_error = None
            var.completed_at = datetime.utcnow()
            db.commit()
            _maybe_complete_campaign(db, var.campaign_id)
        logger.info("ugc_render_job: variation %s rendered", variation_id)
    except Exception as e:  # noqa: BLE001
        logger.exception("ugc_render_job failed for variation %s", variation_id)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        var = db.query(UgcVariation).filter(UgcVariation.id == variation_id).first()
        if var is not None:
            var.status = "failed"
            var.render_error = str(e)[:4000]
            db.commit()
            # Refund the credit reserved for this variation.
            campaign = db.query(UgcCampaign).filter(UgcCampaign.id == var.campaign_id).first()
            if campaign is not None:
                ugc_credits.refund(
                    db,
                    campaign.workspace_id,
                    ugc_credits.credit_cost_per_variation(),
                    variation_id=variation_id,
                )
    finally:
        db.close()


def _maybe_complete_campaign(db: Session, campaign_id: int) -> None:
    """Flip campaign to 'completed' once no variations remain pending/rendering."""
    pending = (
        db.query(UgcVariation)
        .filter(
            UgcVariation.campaign_id == campaign_id,
            UgcVariation.status.in_(("draft", "queued", "rendering", "generating")),
        )
        .count()
    )
    if pending == 0:
        campaign = db.query(UgcCampaign).filter(UgcCampaign.id == campaign_id).first()
        if campaign is not None and campaign.status != "completed":
            campaign.status = "completed"
            db.commit()
