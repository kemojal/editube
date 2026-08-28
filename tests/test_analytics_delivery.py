from datetime import datetime, timedelta

import httpx
from sqlalchemy.orm import sessionmaker

from app.db.models import AnalyticsOutbox
from app.jobs.analytics_delivery import (
    MAX_ATTEMPTS,
    _claim_rows,
    _posthog_payload,
    deliver_analytics_batch,
)
from app.services.product_analytics import emit, emit_after_commit


def _pending_event(db_session, event_id: str) -> AnalyticsOutbox:
    row = emit(
        db_session,
        "feature_completed",
        properties={"feature_key": "rough_cut", "result": "success"},
        event_id=event_id,
    )
    db_session.commit()
    return row


def test_posthog_payload_has_stable_provider_insert_id(db_session):
    row = _pending_event(db_session, "delivery-stable-id")
    first = _posthog_payload(row)
    second = _posthog_payload(row)

    assert first == second
    assert first["properties"]["$insert_id"] == "delivery-stable-id"


def test_delivery_timeout_applies_exponential_backoff(
    db_session, monkeypatch
):
    row = _pending_event(db_session, "delivery-timeout-id")
    monkeypatch.setenv("POSTHOG_PROJECT_API_KEY", "phc_test")
    monkeypatch.setattr("app.jobs.analytics_delivery.SessionLocal", lambda: db_session)
    monkeypatch.setattr(
        "app.jobs.analytics_delivery.httpx.post",
        lambda *args, **kwargs: (_ for _ in ()).throw(httpx.ReadTimeout("timeout")),
    )

    result = deliver_analytics_batch()

    row = db_session.get(AnalyticsOutbox, "delivery-timeout-id")
    assert row is not None
    assert result["status"] == "timeout"
    assert row.delivery_status == "failed"
    assert row.attempt_count == 1
    assert row.last_error_code == "provider_timeout"
    assert row.next_attempt_at is not None


def test_delivery_moves_exhausted_rows_to_dead_letter(db_session, monkeypatch):
    row = _pending_event(db_session, "delivery-dead-letter-id")
    row.attempt_count = MAX_ATTEMPTS - 1
    db_session.commit()
    monkeypatch.setenv("POSTHOG_PROJECT_API_KEY", "phc_test")
    monkeypatch.setattr("app.jobs.analytics_delivery.SessionLocal", lambda: db_session)
    monkeypatch.setattr(
        "app.jobs.analytics_delivery.httpx.post",
        lambda *args, **kwargs: (_ for _ in ()).throw(httpx.ReadTimeout("timeout")),
    )

    deliver_analytics_batch()

    row = db_session.get(AnalyticsOutbox, "delivery-dead-letter-id")
    assert row is not None
    assert row.attempt_count == MAX_ATTEMPTS
    assert row.delivery_status == "dead_letter"
    assert row.next_attempt_at is None


def test_delivery_success_clears_retry_state(db_session, monkeypatch):
    row = _pending_event(db_session, "delivery-retry-success-id")
    row.delivery_status = "failed"
    row.attempt_count = 2
    row.next_attempt_at = datetime.utcnow() - timedelta(seconds=1)
    row.last_error_code = "provider_timeout"
    db_session.commit()
    monkeypatch.setenv("POSTHOG_PROJECT_API_KEY", "phc_test")
    monkeypatch.setattr("app.jobs.analytics_delivery.SessionLocal", lambda: db_session)
    response = httpx.Response(200, request=httpx.Request("POST", "https://example.test/batch"))
    monkeypatch.setattr("app.jobs.analytics_delivery.httpx.post", lambda *args, **kwargs: response)

    result = deliver_analytics_batch()

    row = db_session.get(AnalyticsOutbox, "delivery-retry-success-id")
    assert row is not None
    assert result["status"] == "delivered"
    assert row.delivery_status == "delivered"
    assert row.attempt_count == 3
    assert row.delivered_at is not None
    assert row.last_error_code is None
    assert row.next_attempt_at is None


def test_stale_delivery_claim_is_reclaimed(db_session):
    row = _pending_event(db_session, "delivery-stale-claim-id")
    row.delivery_status = "delivering"
    row.delivery_started_at = datetime.utcnow() - timedelta(minutes=11)
    db_session.commit()

    claimed = _claim_rows(db_session, 10)

    assert [item.event_id for item in claimed] == [row.event_id]
    assert row.delivery_status == "delivering"
    assert row.last_error_code == "stale_delivery_claim"
    assert row.attempt_count == 1


def test_deterministic_event_id_is_exactly_once_at_outbox_edge(
    db_session, monkeypatch
):
    factory = sessionmaker(bind=db_session.get_bind(), autocommit=False, autoflush=False)
    monkeypatch.setattr("app.services.product_analytics.SessionLocal", factory)

    first = emit_after_commit(
        "job_completed",
        source="worker",
        properties={"job_id": "job-1", "job_type": "render", "result": "success"},
        event_id="rq:job-1:1:completed",
    )
    second = emit_after_commit(
        "job_completed",
        source="worker",
        properties={"job_id": "job-1", "job_type": "render", "result": "success"},
        event_id="rq:job-1:1:completed",
    )

    assert first == second == "rq:job-1:1:completed"
    assert (
        db_session.query(AnalyticsOutbox)
        .filter(AnalyticsOutbox.event_id == "rq:job-1:1:completed")
        .count()
        == 1
    )
