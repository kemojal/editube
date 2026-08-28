"""RQ job: generate brief + hooks/scripts/CTAs for a product."""

from __future__ import annotations

import logging

from app.db.database import SessionLocal
from app.db.models import UgcBrief, UgcProduct
from app.services.ugc_brief import generate_brief
from app.services.ugc_creative import generate_ctas, generate_hooks, generate_scripts

logger = logging.getLogger(__name__)


def _product_dict(p: UgcProduct) -> dict:
    return {
        "name": p.name,
        "brand": p.brand,
        "price": p.price,
        "currency": p.currency,
        "description": p.description,
        "benefits": list(p.benefits or []),
        "pain_points": list(p.pain_points or []),
        "use_cases": list(p.use_cases or []),
        "reviews": list(p.reviews or []),
        "target_audience": p.target_audience,
    }


def ugc_brief_generate_job(product_id: int) -> None:
    db = SessionLocal()
    try:
        product = db.query(UgcProduct).filter(UgcProduct.id == product_id).first()
        if product is None:
            raise RuntimeError(f"UGC product {product_id} was removed before brief generation")
        brief = (
            db.query(UgcBrief)
            .filter(UgcBrief.product_id == product_id)
            .order_by(UgcBrief.id.desc())
            .first()
        )
        if brief is None:
            brief = UgcBrief(product_id=product_id, status="processing")
            db.add(brief)
            db.flush()
        else:
            brief.status = "processing"
            brief.error_message = None
            db.commit()

        try:
            pdict = _product_dict(product)
            b = generate_brief(pdict)
            hooks = generate_hooks(b, pdict, 20)
            scripts = generate_scripts(b, pdict, b.get("angles"), 10)
            ctas = generate_ctas(b, pdict, 3)

            brief.audience = b.get("audience")
            brief.main_promise = b.get("main_promise")
            brief.pain_points = b.get("pain_points") or []
            brief.objections = b.get("objections") or []
            brief.benefits = b.get("benefits") or []
            brief.angles = b.get("angles") or []
            brief.hooks = hooks
            brief.scripts = scripts
            brief.ctas = ctas
            brief.status = "ready"
            brief.error_message = None
            db.commit()
            logger.info("ugc_brief_generate_job: brief %s ready (product %s)", brief.id, product_id)
        except Exception as e:  # noqa: BLE001
            logger.exception("ugc_brief_generate_job failed for product %s", product_id)
            db.rollback()
            brief = (
                db.query(UgcBrief).filter(UgcBrief.product_id == product_id).order_by(UgcBrief.id.desc()).first()
            )
            if brief is not None:
                brief.status = "failed"
                brief.error_message = str(e)[:4000]
                db.commit()
            raise
    finally:
        db.close()
