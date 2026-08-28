from __future__ import annotations

from cryptography.fernet import Fernet
from datetime import datetime, timedelta
import json
import re
from urllib.parse import unquote
import httpx
import pytest

from app.db.models import (
    AnalyticsConsent,
    AnalyticsConsentEvent,
    AnalyticsDataRequest,
    AnalyticsFeedback,
    AnalyticsOutbox,
    ApiToken,
    CheckoutAttempt,
    Subscription,
    SubscriptionLifecycleEvent,
    Workspace,
    WorkspaceActivation,
    WorkspaceMember,
    UserSession,
    User,
)
from app.services.activation_analytics import record_first_value
from app.services.analytics_data_rights import request_analytics_deletion
from app.services.analytics_retention import apply_analytics_retention
from app.services.checkout_analytics import model_mature_checkout_abandonment
from app.services.observability import _sentry_dsn_for_role, before_send
from app.services.analytics_privacy import AnalyticsPrivacyError, sanitize_properties
from app.services.product_analytics import emit, emit_once
from app.services.subscription_analytics import record_subscription_lifecycle
from app.jobs.analytics_privacy import _delete_posthog_person
from app.utils.security import previous_user_activity_gap_days
from app.utils.security import authenticate_api_token, get_password_hash, hash_api_token, verify_password
from app.services.ingest_service import record_ingested_video_result_use
from app.rq_worker import _returned_job_status
from app.jobs.queue import _record_enqueued


def _consent_payload(consent_id: str, **overrides):
    payload = {
        "anonymous_consent_id": consent_id,
        "analytics_enabled": True,
        "replay_enabled": False,
        "product_data_improvement_enabled": False,
        "consent_version": "2026-08-27",
        "region_policy": "default",
        "global_privacy_control": False,
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    ("properties", "message"),
    [
        ({"email": "person@example.test"}, "Prohibited analytics property key"),
        ({"note": "person@example.test"}, "contains prohibited data"),
        ({"authorization_header": "redacted"}, "Prohibited analytics property key"),
        ({"target": "https://example.test/private?id=1"}, "Full URLs are prohibited"),
        ({"api_token_count": 2}, "Prohibited analytics property key"),
        ({"ip": "203.0.113.10"}, "Prohibited analytics property key"),
        ({"url": "relative-but-sensitive"}, "Prohibited analytics property key"),
    ],
)
def test_analytics_properties_fail_closed(properties, message):
    with pytest.raises(AnalyticsPrivacyError, match=message):
        sanitize_properties(properties)


def test_emit_persists_only_registered_safe_events(db_session, make_user):
    user = make_user(plan="pro", subscription_status="active")

    row = emit(
        db_session,
        "project_created",
        user=user,
        properties={"feature_key": "media_import", "source_type": "upload"},
        event_id="evt-analytics-safe-0001",
    )
    db_session.commit()

    stored = db_session.get(AnalyticsOutbox, row.event_id)
    assert stored is not None
    assert stored.user_id == user.id
    assert stored.properties == {
        "feature_key": "media_import",
        "source_type": "upload",
        "plan": "pro",
        "subscription_status": "active",
        "user_role": "creator",
    }
    with pytest.raises(ValueError, match="Unknown analytics event"):
        emit(db_session, "made_up_event", user=user)


def test_emit_once_deduplicates_authoritative_terminal_event(db_session, make_user):
    user = make_user()

    first = emit_once(
        db_session,
        "job_canceled",
        event_id="job:export:41:canceled",
        user=user,
        properties={"job_kind": "export", "job_id": "41", "feature_key": "export"},
    )
    duplicate = emit_once(
        db_session,
        "job_canceled",
        event_id="job:export:41:canceled",
        user=user,
        properties={"job_kind": "export", "job_id": "41", "feature_key": "export"},
    )
    db_session.commit()

    assert first is not None
    assert duplicate is first
    assert db_session.query(AnalyticsOutbox).filter_by(event_name="job_canceled").count() == 1


def test_dashboard_first_view_is_cross_device_idempotent(
    api_client,
    db_session,
    make_user,
):
    created_at = datetime.utcnow() - timedelta(hours=3)
    user = make_user(created_at=created_at)
    api_client.login(user)

    first = api_client.post("/analytics/dashboard-first-view")
    second = api_client.post("/analytics/dashboard-first-view")

    assert first.status_code == 200
    assert first.json()["first_view"] is True
    assert first.json()["time_since_account_creation_ms"] >= 3 * 60 * 60 * 1000
    assert second.status_code == 200
    assert second.json()["first_view"] is False
    db_session.expire_all()
    assert db_session.get(User, user.id).first_dashboard_viewed_at is not None
    events = (
        db_session.query(AnalyticsOutbox)
        .filter(AnalyticsOutbox.event_name == "dashboard_first_viewed")
        .all()
    )
    assert len(events) == 1
    assert events[0].event_id == f"user:{user.id}:dashboard:first-view"


def test_api_token_first_use_records_result_once(
    db_session, make_user, monkeypatch
):
    user = make_user()
    raw_token = "edt_test_token_for_analytics"
    token = ApiToken(
        user_id=user.id,
        name="Automation",
        token_prefix=raw_token[:12],
        token_hash=hash_api_token(raw_token),
    )
    db_session.add(token)
    db_session.commit()
    calls = []
    monkeypatch.setattr(
        "app.services.product_analytics.emit_after_commit",
        lambda event_name, **kwargs: calls.append((event_name, kwargs)),
    )

    assert authenticate_api_token(db_session, raw_token).id == user.id
    assert authenticate_api_token(db_session, raw_token).id == user.id

    assert len(calls) == 1
    assert calls[0][0] == "feature_result_used"
    assert calls[0][1]["event_id"] == f"api-token:{token.id}:first-use"
    assert calls[0][1]["properties"] == {
        "feature_key": "api_tokens",
        "result_action": "authenticated_api_request",
        "result": "success",
    }


def test_watch_folder_result_use_is_attributed_without_leaking_local_path(
    make_video, monkeypatch
):
    video = make_video(
        ingest_source="watch_folder",
        description="Auto-uploaded from watch folder: /private/client/secret.mov",
    )
    calls = []
    monkeypatch.setattr(
        "app.services.product_analytics.emit_after_commit",
        lambda event_name, **kwargs: calls.append((event_name, kwargs)),
    )

    record_ingested_video_result_use(video=video, user_id=video.uploader_id, workspace_id=7)

    assert len(calls) == 1
    event_name, kwargs = calls[0]
    assert event_name == "feature_result_used"
    assert kwargs["event_id"] == f"feature:watch-folder:video:{video.id}:first-open"
    serialized = json.dumps(kwargs["properties"])
    assert "/private/client" not in serialized
    assert "description" not in kwargs["properties"]


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"status": "failed"}, "failed"),
        ({"status": "cancelled"}, "cancelled"),
        ({"status": "ready"}, "ready"),
        (None, None),
    ],
)
def test_rq_worker_reads_explicit_terminal_result_status(result, expected):
    class FakeJob:
        def return_value(self):
            return result

    assert _returned_job_status(FakeJob()) == expected


def test_queue_publication_event_has_real_job_id_and_resource_attribution(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.jobs.queue.persisted_job_context",
        lambda job_type, resource_id: {
            "user_id": 12,
            "workspace_id": 34,
            "project_id": 56,
            "video_id": 78,
            "status": "queued",
        },
    )
    monkeypatch.setattr(
        "app.jobs.queue.emit_after_commit",
        lambda event_name, **kwargs: calls.append((event_name, kwargs)),
    )

    class FakeJob:
        id = "rq-real-id"
        origin = "media"

    _record_enqueued(FakeJob(), "rough_cut_export_job", 91)

    assert calls == [
        (
            "job_queued",
            {
                "user_id": 12,
                "workspace_id": 34,
                "properties": {
                    "job_id": "rq-real-id",
                    "job_type": "rough_cut_export_job",
                    "queue": "media",
                    "resource_id": 91,
                    "feature_key": "export",
                    "project_id": 56,
                    "video_id": 78,
                },
                "event_id": "rq:rq-real-id:queued",
            },
        )
    ]


def test_previous_user_activity_gap_uses_server_session_history(db_session, make_user):
    user = make_user()
    db_session.add(
        UserSession(
            user_id=user.id,
            session_id="historical-session",
            last_activity_at=datetime.utcnow() - timedelta(days=21, hours=2),
        )
    )
    db_session.commit()

    assert previous_user_activity_gap_days(db_session, user.id) == 21


def test_consent_is_audited_and_gpc_forces_essential_only(api_client, db_session):
    consent_id = "device-consent-id-00000001"
    response = api_client.put(
        "/analytics/consent",
        headers={"Sec-GPC": "1"},
        json=_consent_payload(consent_id, replay_enabled=True),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["consent_state"] == "essential_only"
    assert body["analytics_enabled"] is False
    assert body["replay_enabled"] is False
    audit = db_session.query(AnalyticsConsentEvent).one()
    assert audit.global_privacy_control is True
    assert db_session.query(AnalyticsOutbox).count() == 0


def test_consent_identifier_cannot_be_claimed_by_another_visitor(api_client, db_session):
    consent_id = "device-consent-id-00000002"
    db_session.add(
        AnalyticsConsent(
            user_id=999,
            anonymous_consent_id=consent_id,
            consent_version="2026-08-27",
        )
    )
    db_session.commit()

    get_response = api_client.get(
        "/analytics/consent",
        params={"anonymous_consent_id": consent_id},
    )
    put_response = api_client.put(
        "/analytics/consent",
        json=_consent_payload(consent_id),
    )

    assert get_response.status_code == 200
    assert get_response.json() is None
    assert put_response.status_code == 409


def test_feedback_free_text_is_encrypted_and_never_copied_to_outbox(
    api_client,
    db_session,
    make_user,
    monkeypatch,
):
    monkeypatch.setenv("ANALYTICS_FEEDBACK_ENCRYPTION_KEY", Fernet.generate_key().decode())
    user = make_user()
    api_client.login(user)

    response = api_client.post(
        "/analytics/feedback",
        json={
            "prompt_key": "checkout_canceled",
            "reason_code": "price",
            "comment": "I need annual invoicing before I can buy.",
            "route_template": "/pricing",
            "feature_key": "invoices",
        },
    )

    assert response.status_code == 201, response.text
    feedback = db_session.query(AnalyticsFeedback).one()
    assert feedback.comment_encrypted
    assert "annual invoicing" not in feedback.comment_encrypted
    event = db_session.query(AnalyticsOutbox).one()
    assert event.event_name == "abandonment_feedback_submitted"
    assert event.properties["reason_code"] == "price"
    assert event.properties["has_comment"] is True
    assert "comment" not in event.properties


def test_feedback_is_server_capped_for_30_days_unless_user_initiated(
    api_client,
    db_session,
    make_user,
    monkeypatch,
):
    monkeypatch.setenv("ANALYTICS_FEEDBACK_ENCRYPTION_KEY", Fernet.generate_key().decode())
    user = make_user()
    api_client.login(user)
    base = {
        "reason_code": "too_complex",
        "route_template": "/onboarding",
    }

    first = api_client.post(
        "/analytics/feedback",
        json={**base, "prompt_key": "onboarding_abandonment"},
    )
    capped = api_client.post(
        "/analytics/feedback",
        json={**base, "prompt_key": "project_creation_abandonment"},
    )
    initiated = api_client.post(
        "/analytics/feedback",
        json={
            **base,
            "prompt_key": "subscription_canceled",
            "user_initiated": True,
        },
    )

    assert first.status_code == 201
    assert first.json()["accepted"] is True
    assert capped.status_code == 201
    assert capped.json()["accepted"] is False
    assert capped.json()["id"] == first.json()["id"]
    assert initiated.status_code == 201
    assert initiated.json()["accepted"] is True
    assert db_session.query(AnalyticsFeedback).count() == 2


def test_password_reset_is_generic_one_time_and_revokes_sessions(
    api_client,
    db_session,
    make_user,
    monkeypatch,
):
    from app.api.routes import users as users_route

    delivered: dict[str, str] = {}

    def fake_send(recipient: str, subject: str, body: str) -> bool:
        delivered.update(recipient=recipient, subject=subject, body=body)
        return True

    monkeypatch.setattr(users_route, "send_transactional_email", fake_send)
    user = make_user(
        email="reset-user@example.com",
        hashed_password=get_password_hash("old-password"),
    )
    session = UserSession(user_id=user.id, session_id="reset-me")
    db_session.add(session)
    db_session.commit()

    unknown = api_client.post(
        "/users/password/forgot",
        json={"email": "nobody@example.com"},
    )
    requested = api_client.post(
        "/users/password/forgot",
        json={"email": user.email.upper()},
    )

    assert unknown.status_code == 200
    assert requested.status_code == 200
    assert unknown.json() == requested.json()
    match = re.search(r"[?&]token=([^\s&]+)", delivered["body"])
    assert match is not None
    token = unquote(match.group(1))

    completed = api_client.post(
        "/users/password/reset",
        json={"token": token, "password": "new-password"},
    )
    replay = api_client.post(
        "/users/password/reset",
        json={"token": token, "password": "another-password"},
    )

    db_session.refresh(user)
    db_session.refresh(session)
    assert completed.status_code == 200
    assert replay.status_code == 400
    assert verify_password("new-password", user.hashed_password)
    assert session.revoked is True
    assert [
        event.event_name
        for event in db_session.query(AnalyticsOutbox)
        .filter(AnalyticsOutbox.event_name.like("password_reset_%"))
        .order_by(AnalyticsOutbox.created_at.asc())
        .all()
    ] == ["password_reset_requested", "password_reset_completed"]


def test_request_correlation_id_is_validated_and_returned(api_client):
    accepted = api_client.get("/health", headers={"X-Request-ID": "request-client-0001"})
    replaced = api_client.get("/health", headers={"X-Request-ID": "bad"})

    assert accepted.status_code == 200
    assert accepted.headers["X-Request-ID"] == "request-client-0001"
    assert replaced.status_code == 200
    assert replaced.headers["X-Request-ID"] != "bad"
    assert len(replaced.headers["X-Request-ID"]) >= 8


def test_first_value_is_recorded_exactly_once_per_workspace(db_session, make_user):
    user = make_user()
    workspace = Workspace(name="Analytics QA", slug="analytics-qa", owner_user_id=user.id)
    db_session.add(workspace)
    db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="owner"))
    db_session.flush()

    first = record_first_value(
        db_session,
        user=user,
        workspace_id=workspace.id,
        feature_key="rough_cut",
        resource_type="video",
        resource_id=41,
    )
    duplicate = record_first_value(
        db_session,
        user=user,
        workspace_id=workspace.id,
        feature_key="export",
        resource_type="video",
        resource_id=42,
    )
    db_session.commit()

    assert first is not None
    assert duplicate is None
    assert db_session.query(WorkspaceActivation).count() == 1
    events = db_session.query(AnalyticsOutbox).all()
    assert [event.event_name for event in events] == ["first_value_achieved"]
    assert events[0].properties["feature_key"] == "rough_cut"


def test_subscription_lifecycle_is_idempotent_and_matches_outbox(db_session, make_user):
    user = make_user(plan="pro", subscription_status="active")
    subscription = Subscription(
        user_id=user.id,
        stripe_subscription_id="sub_analytics_001",
        status="active",
        plan="pro",
        currency="usd",
        unit_amount=2400,
        quantity=2,
        recurring_interval="month",
    )
    db_session.add(subscription)
    db_session.flush()

    created = record_subscription_lifecycle(
        db_session,
        event_type="subscription_activated",
        user=user,
        subscription=subscription,
        source_event_id="evt_stripe_analytics_001",
    )
    duplicate = record_subscription_lifecycle(
        db_session,
        event_type="subscription_activated",
        user=user,
        subscription=subscription,
        source_event_id="evt_stripe_analytics_001",
    )
    db_session.commit()

    assert created is not None
    assert duplicate is None
    assert db_session.query(SubscriptionLifecycleEvent).count() == 1
    event = db_session.query(AnalyticsOutbox).one()
    assert event.event_name == "subscription_activated"
    assert event.properties["amount_minor"] == 4800


def test_deletion_removes_restricted_rows_and_all_resource_identifiers(
    db_session,
    make_user,
    monkeypatch,
):
    monkeypatch.delenv("POSTHOG_PROJECT_API_KEY", raising=False)
    monkeypatch.delenv("POSTHOG_PROJECT_ID", raising=False)
    monkeypatch.delenv("POSTHOG_PERSONAL_API_KEY", raising=False)
    user = make_user()
    db_session.add(
        AnalyticsConsent(
            user_id=user.id,
            anonymous_consent_id="device-consent-id-delete-001",
            analytics_enabled=True,
            consent_version="2026-08-27",
        )
    )
    db_session.add(
        AnalyticsFeedback(
            user_id=user.id,
            prompt_key="checkout_canceled",
            reason_code="price",
            comment_encrypted="encrypted",
        )
    )
    event = emit(
        db_session,
        "feature_completed",
        user=user,
        properties={
            "feature_key": "rough_cut",
            "project_id": 10,
            "video_id": 20,
            "analytics_session_id": "session-private-001",
            "result": "success",
        },
    )
    checkout_attempt = CheckoutAttempt(
        user_id=user.id,
        stripe_checkout_session_id="cs_private_delete_001",
        plan="pro",
        recurring_interval="month",
        campaign_id="campaign-private-001",
    )
    db_session.add(checkout_attempt)
    db_session.flush()

    request_row = request_analytics_deletion(db_session, user_id=user.id)
    db_session.commit()

    assert request_row.status == "completed"
    assert request_row.user_id is None
    assert db_session.query(AnalyticsConsent).count() == 0
    assert db_session.query(AnalyticsFeedback).count() == 0
    db_session.refresh(event)
    assert event.user_id is None
    assert event.workspace_id is None
    assert event.properties == {
        "feature_key": "rough_cut",
        "result": "success",
        "user_role": "creator",
    }
    db_session.refresh(checkout_attempt)
    assert checkout_attempt.user_id is None
    assert checkout_attempt.workspace_id is None
    assert checkout_attempt.campaign_id is None
    assert checkout_attempt.stripe_checkout_session_id.startswith("deleted:")
    assert "cs_private_delete_001" not in checkout_attempt.stripe_checkout_session_id


def test_checkout_abandonment_requires_maturity_and_is_exactly_once(
    db_session,
    make_user,
):
    now = datetime(2026, 8, 29, 12, 0, 0)
    user = make_user(plan="pro")
    mature = CheckoutAttempt(
        user_id=user.id,
        stripe_checkout_session_id="cs_private_mature_001",
        plan="pro",
        recurring_interval="month",
        campaign_id="summer-safe",
        source="billing_checkout",
        trial_days=14,
        offer_applied=True,
        created_at=now - timedelta(hours=25),
    )
    recent = CheckoutAttempt(
        user_id=user.id,
        stripe_checkout_session_id="cs_private_recent_001",
        plan="pro",
        recurring_interval="year",
        created_at=now - timedelta(hours=23),
    )
    completed = CheckoutAttempt(
        user_id=user.id,
        stripe_checkout_session_id="cs_private_completed_001",
        plan="pro",
        recurring_interval="month",
        status="completed",
        completed_at=now - timedelta(hours=1),
        created_at=now - timedelta(hours=48),
    )
    canceled = CheckoutAttempt(
        user_id=user.id,
        stripe_checkout_session_id="cs_private_canceled_001",
        plan="pro",
        recurring_interval="month",
        status="canceled",
        canceled_at=now - timedelta(hours=30),
        created_at=now - timedelta(hours=48),
    )
    db_session.add_all((mature, recent, completed, canceled))
    db_session.commit()

    assert model_mature_checkout_abandonment(db_session, now=now) == 1
    db_session.commit()
    assert model_mature_checkout_abandonment(db_session, now=now) == 0
    db_session.commit()

    db_session.refresh(mature)
    db_session.refresh(recent)
    db_session.refresh(completed)
    db_session.refresh(canceled)
    assert mature.status == "abandoned"
    assert mature.abandoned_at == now
    assert recent.status == "created"
    assert completed.status == "completed"
    assert canceled.status == "canceled"

    rows = db_session.query(AnalyticsOutbox).filter_by(event_name="checkout_abandoned").all()
    assert len(rows) == 1
    assert rows[0].event_id == f"checkout-attempt:{mature.id}:abandoned"
    assert rows[0].source == "modeled"
    assert rows[0].properties["maturity_hours"] == 24
    assert rows[0].properties["trial_offered"] is True
    serialized = json.dumps(rows[0].properties)
    assert "cs_private" not in serialized


def test_posthog_deletion_waits_for_events_and_recordings(monkeypatch):
    monkeypatch.setenv("POSTHOG_PROJECT_ID", "123")
    monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_personal")
    real_client = httpx.Client
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "GET" and request.url.path.endswith("/persons/"):
            return httpx.Response(200, json={"results": [{"id": "person-uuid-1"}]})
        if request.method == "POST" and request.url.path.endswith("/persons/bulk_delete/"):
            assert json.loads(request.content) == {
                "distinct_ids": ["42"],
                "delete_events": True,
                "delete_recordings": True,
            }
            return httpx.Response(
                202,
                json={
                    "persons_found": 1,
                    "persons_deleted": 1,
                    "events_queued_for_deletion": True,
                    "recordings_queued_for_deletion": True,
                    "deletion_errors": [],
                },
            )
        if request.method == "GET" and request.url.path.endswith("/persons/deletion_status/"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"person_uuid": "person-uuid-1", "status": "completed"}
                    ]
                },
            )
        raise AssertionError(f"Unexpected provider request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "app.jobs.analytics_privacy.httpx.Client",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    queued, details = _delete_posthog_person("42")
    completed, final_details = _delete_posthog_person(
        "42", {"posthog": queued, **details}
    )

    assert queued == "processing"
    assert details == {"posthog_person_uuids": ["person-uuid-1"]}
    assert completed == "deleted"
    assert final_details == details
    assert calls == [
        "GET /api/projects/123/persons/",
        "POST /api/projects/123/persons/bulk_delete/",
        "GET /api/projects/123/persons/deletion_status/",
    ]


def test_deletion_request_status_remains_available_after_anonymization(
    api_client,
    make_user,
    monkeypatch,
):
    for name in (
        "POSTHOG_PROJECT_API_KEY",
        "POSTHOG_PROJECT_ID",
        "POSTHOG_PERSONAL_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    owner = make_user(email="privacy-owner@example.test")
    other = make_user(email="privacy-other@example.test")
    api_client.login(owner)

    created = api_client.delete("/analytics/me")
    assert created.status_code == 202
    request_id = created.json()["request_id"]
    assert created.json()["status"] == "completed"

    status = api_client.get(f"/analytics/me/deletions/{request_id}")
    assert status.status_code == 200
    assert status.json()["provider_status"]["first_party"] == "deleted"

    api_client.login(other)
    denied = api_client.get(f"/analytics/me/deletions/{request_id}")
    assert denied.status_code == 404


def test_deletion_suppresses_undelivered_outbox_rows(
    db_session,
    make_user,
    monkeypatch,
):
    for name in (
        "POSTHOG_PROJECT_API_KEY",
        "POSTHOG_PROJECT_ID",
        "POSTHOG_PERSONAL_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    user = make_user()
    pending = emit(
        db_session,
        "project_created",
        user=user,
        properties={"source_type": "upload", "project_id": 123},
    )
    failed = emit(
        db_session,
        "feature_failed",
        user=user,
        properties={"feature_key": "export", "error_code": "render_failed"},
    )
    failed.delivery_status = "failed"
    db_session.commit()

    request_analytics_deletion(db_session, user_id=user.id)
    db_session.commit()

    for event_id in (pending.event_id, failed.event_id):
        row = db_session.get(AnalyticsOutbox, event_id)
        assert row is not None
        assert row.delivery_status == "suppressed"
        assert row.last_error_code == "privacy_deletion"
        assert row.user_id is None
        assert row.workspace_id is None
        assert row.anonymous_id is None
        assert "project_id" not in row.properties


def test_sentry_scrubber_removes_error_surface_secrets_and_pii():
    clean = before_send(
        {
            "message": "Failure for person@example.test at https://example.test/file?token=secret",
            "request": {
                "url": "https://api.example.test/projects/1?token=secret",
                "data": {"transcript": "TRANSCRIPT_SENTINEL"},
                "cookies": {"session": "secret"},
                "headers": {"Authorization": "Bearer secret", "X-Request-ID": "request-1"},
            },
            "user": {"id": "42", "email": "person@example.test"},
            "extra": {"safe": "Bearer secret", "prompt_text": "private"},
            "breadcrumbs": [{"message": "Open https://example.test/private"}],
            "exception": {
                "values": [
                    {
                        "type": "RuntimeError",
                        "value": "Failed for person@example.test",
                        "stacktrace": {
                            "frames": [
                                {
                                    "filename": "https://app.example.test/assets/app.js?token=secret#frame",
                                    "abs_path": "https://app.example.test/assets/app.js?token=secret",
                                    "function": "render",
                                    "lineno": 42,
                                }
                            ]
                        },
                    }
                ]
            },
        }
    )

    serialized = str(clean)
    assert "person@example.test" not in serialized
    assert "TRANSCRIPT_SENTINEL" not in serialized
    assert "token=secret" not in serialized
    assert "Bearer secret" not in serialized
    assert clean["request"]["headers"] == {"X-Request-ID": "request-1"}
    assert clean["user"] == {"id": "42"}
    frame = clean["exception"]["values"][0]["stacktrace"]["frames"][0]
    assert frame["filename"] == "https://app.example.test/assets/app.js"
    assert frame["abs_path"] == "https://app.example.test/assets/app.js"
    assert frame["function"] == "render"
    assert frame["lineno"] == 42


def test_sentry_uses_role_specific_dsn_with_shared_fallback(monkeypatch):
    monkeypatch.setenv("SENTRY_DSN", "https://shared.example/1")
    monkeypatch.setenv("SENTRY_API_DSN", "https://api.example/2")
    monkeypatch.setenv("SENTRY_WORKER_DSN", "https://worker.example/3")

    assert _sentry_dsn_for_role("api") == "https://api.example/2"
    assert _sentry_dsn_for_role("worker") == "https://worker.example/3"

    monkeypatch.delenv("SENTRY_WORKER_DSN")
    assert _sentry_dsn_for_role("worker") == "https://shared.example/1"


def test_retention_deletes_expired_analytics_but_never_pending_delivery(
    db_session,
    make_user,
):
    now = datetime(2026, 8, 27, 12, 0, 0)
    old = now - timedelta(days=800)
    user = make_user()
    delivered = emit(
        db_session,
        "project_created",
        user=user,
        occurred_at=old,
        properties={"source_type": "upload"},
    )
    delivered.delivery_status = "delivered"
    pending = emit(
        db_session,
        "project_created",
        user=user,
        occurred_at=old,
        properties={"source_type": "upload"},
    )
    db_session.add(
        AnalyticsFeedback(
            user_id=user.id,
            prompt_key="checkout_canceled",
            reason_code="price",
            created_at=old,
        )
    )
    db_session.add(
        AnalyticsConsentEvent(
            user_id=user.id,
            anonymous_consent_id="retention-consent-id-0001",
            consent_state="essential_only",
            analytics_enabled=False,
            replay_enabled=False,
            product_data_improvement_enabled=False,
            consent_version="2026-08-27",
            region_policy="default",
            global_privacy_control=False,
            occurred_at=old,
        )
    )
    db_session.add(
        AnalyticsDataRequest(
            user_id=None,
            distinct_id="deleted:hash",
            request_type="delete",
            status="completed",
            completed_at=old,
        )
    )
    db_session.commit()
    delivered_event_id = delivered.event_id
    pending_event_id = pending.event_id

    result = apply_analytics_retention(db_session, now=now)

    assert result == {
        "outbox_deleted": 1,
        "feedback_deleted": 1,
        "consent_events_deleted": 1,
        "data_requests_deleted": 1,
        "checkout_attempts_deleted": 0,
    }
    assert db_session.get(AnalyticsOutbox, delivered_event_id) is None
    assert db_session.get(AnalyticsOutbox, pending_event_id) is not None
