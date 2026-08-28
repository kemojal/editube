"""RQ job: extract a product from its URL and populate the UgcProduct row."""

from __future__ import annotations

import logging

from app.db.database import SessionLocal
from app.db.models import UgcProduct
from app.services.ugc_import import import_product

logger = logging.getLogger(__name__)


def ugc_product_import_job(product_id: int) -> None:
    db = SessionLocal()
    try:
        product = db.query(UgcProduct).filter(UgcProduct.id == product_id).first()
        if product is None:
            raise RuntimeError(f"UGC product {product_id} was removed before import started")
        try:
            data = import_product(product.source_url)
            product.source_type = data.get("source_type") or product.source_type
            product.name = data.get("name") or product.name
            product.brand = data.get("brand")
            product.price = data.get("price")
            product.currency = data.get("currency")
            product.description = data.get("description")
            product.benefits = data.get("benefits") or []
            product.pain_points = data.get("pain_points") or []
            product.use_cases = data.get("use_cases") or []
            product.target_audience = data.get("target_audience")
            product.reviews = data.get("reviews") or []
            product.image_urls = data.get("image_urls") or []
            product.raw_scrape = data.get("raw_scrape")
            product.status = "ready"
            product.error_message = None
            db.commit()
            logger.info("ugc_product_import_job: product %s ready", product_id)
        except Exception as e:  # noqa: BLE001
            logger.exception("ugc_product_import_job failed for product %s", product_id)
            db.rollback()
            product = db.query(UgcProduct).filter(UgcProduct.id == product_id).first()
            if product is not None:
                product.status = "failed"
                product.error_message = str(e)[:4000]
                db.commit()
            raise
    finally:
        db.close()
