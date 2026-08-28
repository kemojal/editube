from datetime import datetime, timedelta

from app.db.models import AnalyticsOutbox, SubscriptionLifecycleEvent
from app.services.analytics_quality import build_analytics_quality_report
from app.services.product_analytics import emit


def _codes(report: dict) -> set[str]:
    return {issue["code"] for issue in report["issues"]}


def test_empty_analytics_quality_report_is_healthy(db_session):
    report = build_analytics_quality_report(
        db_session,
        now=datetime.utcnow(),
    )

    assert report["status"] == "healthy"
    assert report["metrics"]["event_count"] == 0
    assert report["issues"] == []


def test_quality_report_detects_dead_letters_backlog_and_malformed_events(db_session):
    now = datetime.utcnow()
    row = emit(
        db_session,
        "feature_completed",
        properties={},
        occurred_at=now - timedelta(minutes=35),
        event_id="quality-malformed-event",
    )
    row.delivery_status = "dead_letter"
    row.created_at = now - timedelta(minutes=35)
    db_session.commit()

    report = build_analytics_quality_report(db_session, now=now)

    assert report["status"] == "critical"
    assert "outbox_dead_letters" in _codes(report)
    assert "missing_required_properties" in _codes(report)


def test_quality_report_reconciles_subscription_facts_to_outbox(db_session):
    now = datetime.utcnow()
    db_session.add(
        SubscriptionLifecycleEvent(
            event_key="subscription_activated:evt_missing",
            event_type="subscription_activated",
            occurred_at=now,
        )
    )
    db_session.commit()

    report = build_analytics_quality_report(db_session, now=now)

    assert report["status"] == "critical"
    assert report["reconciliation"]["subscription_lifecycle"]["missing_count"] == 1
    assert "subscription_reconciliation_gap" in _codes(report)


def test_quality_endpoint_requires_admin_and_returns_report(api_client, make_user):
    member = make_user(role="creator")
    api_client.login(member)
    assert api_client.get("/analytics/quality").status_code == 403

    admin = make_user(email="analytics-admin@example.test", role="admin")
    api_client.login(admin)
    response = api_client.get("/analytics/quality")

    assert response.status_code == 200
    assert response.json()["status"] in {"healthy", "degraded", "critical"}
    assert "reconciliation" in response.json()
