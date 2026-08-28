"""Process privacy deletion requests against configured analytics providers."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime
from urllib.parse import quote

import httpx

from app.db.database import SessionLocal
from app.db.models import AnalyticsDataRequest
from app.services.observability import observed_span


class ProviderDeletionError(RuntimeError):
    """A provider accepted the request but reported item-level failures."""


def _posthog_url(project_id: str, suffix: str) -> str:
    host = (os.getenv("POSTHOG_API_HOST") or "https://us.posthog.com").strip().rstrip("/")
    return f"{host}/api/projects/{quote(project_id, safe='')}/persons/{suffix.lstrip('/')}"


def _posthog_deletion_complete(
    client: httpx.Client,
    *,
    project_id: str,
    headers: dict[str, str],
    person_uuids: list[str],
) -> bool:
    """Return true only after PostHog verifies every asynchronous event deletion."""
    for person_uuid in person_uuids:
        response = client.get(
            _posthog_url(project_id, "deletion_status/"),
            headers=headers,
            params={"person_uuid": person_uuid, "status": "all", "limit": 10},
        )
        response.raise_for_status()
        results = response.json().get("results") or []
        matching = [
            item
            for item in results
            if str(item.get("person_uuid") or "") == person_uuid
        ]
        # An empty result immediately after enqueue can be eventual consistency,
        # so absence is never interpreted as proof of deletion.
        if not matching or any(item.get("status") != "completed" for item in matching):
            return False
    return True


def _delete_posthog_person(
    distinct_id: str,
    provider_status: dict | None = None,
) -> tuple[str, dict[str, object]]:
    """Queue or verify deletion of person, events, and session recordings.

    PostHog's current bulk-delete endpoint removes the person immediately and
    queues event/recording deletion asynchronously. We retain only provider
    UUIDs until that queue reports completion, then hash the local distinct ID.
    """
    project_id = (os.getenv("POSTHOG_PROJECT_ID") or "").strip()
    personal_key = (os.getenv("POSTHOG_PERSONAL_API_KEY") or "").strip()
    if not project_id or not personal_key:
        return "pending_configuration", {}

    headers = {"Authorization": f"Bearer {personal_key}"}
    previous = provider_status or {}
    person_uuids = [
        str(value)
        for value in (previous.get("posthog_person_uuids") or [])
        if value
    ]
    with httpx.Client(timeout=15) as client:
        if previous.get("posthog") == "processing" and person_uuids:
            complete = _posthog_deletion_complete(
                client,
                project_id=project_id,
                headers=headers,
                person_uuids=person_uuids,
            )
            return (
                "deleted" if complete else "processing",
                {"posthog_person_uuids": person_uuids},
            )

        with observed_span(
            "http.client",
            "GET analytics-provider person",
            provider="posthog",
        ):
            response = client.get(
                _posthog_url(project_id, ""),
                headers=headers,
                params={"distinct_id": distinct_id},
            )
            response.raise_for_status()
        results = response.json().get("results") or []
        person_uuids = [
            str(person.get("id"))
            for person in results
            if person.get("id")
        ]
        if not person_uuids:
            return "not_found", {}

        with observed_span(
            "http.client",
            "POST analytics-provider deletion",
            provider="posthog",
        ):
            deleted = client.post(
                _posthog_url(project_id, "bulk_delete/"),
                headers=headers,
                json={
                    "distinct_ids": [distinct_id],
                    "delete_events": True,
                    "delete_recordings": True,
                },
            )
            deleted.raise_for_status()
        result = deleted.json()
        if result.get("deletion_errors"):
            raise ProviderDeletionError("posthog_item_deletion_failed")
        asynchronous = bool(
            result.get("events_queued_for_deletion")
            or result.get("recordings_queued_for_deletion")
        )
        return (
            "processing" if asynchronous else "deleted",
            {"posthog_person_uuids": person_uuids} if asynchronous else {},
        )


def process_analytics_data_requests(limit: int = 20) -> dict[str, int]:
    db = SessionLocal()
    completed = 0
    processing = 0
    pending_configuration = 0
    failed = 0
    try:
        query = (
            db.query(AnalyticsDataRequest)
            .filter(
                AnalyticsDataRequest.request_type == "delete",
                AnalyticsDataRequest.status.in_(
                    ("pending", "failed", "pending_configuration", "provider_processing")
                ),
            )
            .order_by(AnalyticsDataRequest.requested_at.asc())
            .limit(max(1, min(int(limit), 100)))
        )
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True)
        rows = query.all()
        for row in rows:
            try:
                posthog_status, details = _delete_posthog_person(
                    row.distinct_id,
                    row.provider_status,
                )
                row.provider_status = {
                    "posthog": posthog_status,
                    **details,
                    # Sentry receives pseudonymous IDs only and has no product
                    # person profile; scrubbed errors expire under retention.
                    "sentry": "retention_policy",
                    "first_party": "deleted",
                }
                if posthog_status == "pending_configuration":
                    row.status = "pending_configuration"
                    row.last_error_code = "posthog_deletion_credentials_missing"
                    pending_configuration += 1
                elif posthog_status == "processing":
                    row.status = "provider_processing"
                    row.last_error_code = None
                    processing += 1
                else:
                    row.status = "completed"
                    row.completed_at = datetime.utcnow()
                    row.last_error_code = None
                    row.distinct_id = (
                        "deleted:"
                        + hashlib.sha256(row.distinct_id.encode("utf-8")).hexdigest()
                    )
                    row.user_id = None
                    completed += 1
            except ProviderDeletionError:
                row.status = "failed"
                row.last_error_code = "posthog_item_deletion_failed"
                failed += 1
            except httpx.HTTPStatusError as exc:
                row.status = "failed"
                row.last_error_code = f"posthog_http_{exc.response.status_code}"
                failed += 1
            except httpx.HTTPError:
                row.status = "failed"
                row.last_error_code = "posthog_network_error"
                failed += 1
        db.commit()
        return {
            "completed": completed,
            "processing": processing,
            "pending_configuration": pending_configuration,
            "failed": failed,
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
