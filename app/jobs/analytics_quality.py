"""Scheduled analytics quality monitor."""

from __future__ import annotations

import logging

from app.db.database import SessionLocal
from app.services.analytics_quality import build_analytics_quality_report
from app.services.checkout_analytics import model_mature_checkout_abandonment
from app.services.observability import capture_message


logger = logging.getLogger(__name__)


def analytics_quality_job(window_hours: int = 24) -> dict:
    db = SessionLocal()
    try:
        modeled_abandonments = model_mature_checkout_abandonment(db)
        db.commit()
        report = build_analytics_quality_report(db, window_hours=window_hours)
        report["metrics"]["checkout_abandonments_modeled"] = modeled_abandonments
    finally:
        db.close()
    if modeled_abandonments:
        from app.jobs.queue import enqueue_analytics_delivery_job

        enqueue_analytics_delivery_job()
    if report["status"] != "healthy":
        codes = ",".join(issue["code"] for issue in report["issues"][:10])
        logger.warning("Analytics quality status=%s issues=%s", report["status"], codes)
        capture_message(
            "Analytics quality monitor detected integrity or delivery gaps",
            level="error" if report["status"] == "critical" else "warning",
            monitor="analytics_quality",
            quality_status=report["status"],
            issue_codes=codes,
        )
    return report
