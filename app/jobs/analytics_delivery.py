"""RQ-compatible delivery of transactional analytics outbox rows."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import or_

from app.db.database import SessionLocal
from app.db.models import AnalyticsOutbox
from app.services.observability import observed_span


logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 8


def _utcnow() -> datetime:
    return datetime.utcnow()


def _posthog_payload(row: AnalyticsOutbox) -> dict:
    distinct_id = (
        str(row.user_id)
        if row.user_id is not None
        else row.anonymous_id or f"server:{row.event_id}"
    )
    properties = {
        **(row.properties or {}),
        "distinct_id": distinct_id,
        "$insert_id": row.event_id,
        "event_id": row.event_id,
        "schema_version": row.schema_version,
        "event_source": row.source,
        "environment": row.environment,
        "release": row.release,
    }
    if row.workspace_id is not None:
        properties["workspace_id"] = row.workspace_id
        properties["$groups"] = {"workspace": str(row.workspace_id)}
    return {
        "event": row.event_name,
        "properties": {key: value for key, value in properties.items() if value is not None},
        "timestamp": row.occurred_at.replace(tzinfo=timezone.utc).isoformat(),
    }


def _claim_rows(db, batch_size: int) -> list[AnalyticsOutbox]:  # noqa: ANN001
    now = _utcnow()
    stale = now - timedelta(minutes=10)
    db.query(AnalyticsOutbox).filter(
        AnalyticsOutbox.delivery_status == "delivering",
        AnalyticsOutbox.delivery_started_at < stale,
    ).update(
        {
            AnalyticsOutbox.delivery_status: "failed",
            AnalyticsOutbox.next_attempt_at: now,
            AnalyticsOutbox.delivery_started_at: None,
            AnalyticsOutbox.last_error_code: "stale_delivery_claim",
        },
        synchronize_session=False,
    )
    db.commit()

    query = (
        db.query(AnalyticsOutbox)
        .filter(
            AnalyticsOutbox.delivery_status.in_(("pending", "failed")),
            or_(
                AnalyticsOutbox.next_attempt_at.is_(None),
                AnalyticsOutbox.next_attempt_at <= now,
            ),
        )
        .order_by(AnalyticsOutbox.occurred_at.asc())
        .limit(max(1, min(int(batch_size), 500)))
    )
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    rows = query.all()
    for row in rows:
        row.delivery_status = "delivering"
        row.delivery_started_at = now
        row.attempt_count = int(row.attempt_count or 0) + 1
    db.commit()
    return rows


def _mark_failed(db, rows: list[AnalyticsOutbox], code: str) -> None:  # noqa: ANN001
    now = _utcnow()
    for row in rows:
        attempt = int(row.attempt_count or 1)
        terminal = attempt >= MAX_ATTEMPTS
        row.delivery_status = "dead_letter" if terminal else "failed"
        row.last_error_code = code[:120]
        row.delivery_started_at = None
        row.next_attempt_at = None if terminal else now + timedelta(
            seconds=min(3600, 15 * (2 ** max(0, attempt - 1)))
        )
    db.commit()


def deliver_analytics_batch(batch_size: int = 100) -> dict[str, int | str | bool]:
    api_key = (os.getenv("POSTHOG_PROJECT_API_KEY") or "").strip()
    host = (os.getenv("POSTHOG_HOST") or "https://us.i.posthog.com").strip().rstrip("/")
    if not api_key:
        return {"enabled": False, "claimed": 0, "delivered": 0, "status": "not_configured"}

    db = SessionLocal()
    try:
        rows = _claim_rows(db, batch_size)
        if not rows:
            return {"enabled": True, "claimed": 0, "delivered": 0, "status": "empty"}

        payload = {"api_key": api_key, "batch": [_posthog_payload(row) for row in rows]}
        try:
            with observed_span(
                "http.client",
                "POST analytics-provider batch",
                provider="posthog",
                batch_size=len(rows),
            ):
                response = httpx.post(
                    f"{host}/batch/",
                    json=payload,
                    timeout=float(os.getenv("ANALYTICS_DELIVERY_TIMEOUT_SECONDS", "10")),
                )
                response.raise_for_status()
        except httpx.TimeoutException:
            _mark_failed(db, rows, "provider_timeout")
            return {"enabled": True, "claimed": len(rows), "delivered": 0, "status": "timeout"}
        except httpx.HTTPStatusError as exc:
            _mark_failed(db, rows, f"provider_http_{exc.response.status_code}")
            return {"enabled": True, "claimed": len(rows), "delivered": 0, "status": "http_error"}
        except httpx.HTTPError:
            _mark_failed(db, rows, "provider_network_error")
            return {"enabled": True, "claimed": len(rows), "delivered": 0, "status": "network_error"}

        delivered_at = _utcnow()
        for row in rows:
            row.delivery_status = "delivered"
            row.delivered_at = delivered_at
            row.delivery_started_at = None
            row.next_attempt_at = None
            row.last_error_code = None
        db.commit()
        return {
            "enabled": True,
            "claimed": len(rows),
            "delivered": len(rows),
            "status": "delivered",
        }
    except Exception:
        db.rollback()
        logger.exception("Analytics outbox delivery failed")
        raise
    finally:
        db.close()


def analytics_delivery_job(batch_size: int = 100) -> dict[str, int | str | bool]:
    """Deliver product events only; privacy requests use their own queue job.

    Keeping these workloads independent prevents multiple workers from selecting
    the same deletion request and ensures a provider ingestion outage cannot
    delay a user's data-rights request.
    """
    return deliver_analytics_batch(batch_size=batch_size)
