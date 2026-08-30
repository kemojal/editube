from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.request_logging.routes import _validate_analytics_window


NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("bucket", "window"),
    [
        ("minute", timedelta(hours=24)),
        ("hour", timedelta(days=31)),
        ("day", timedelta(days=31)),
    ],
)
def test_timeline_accepts_each_supported_bucket_at_its_maximum_window(bucket, window):  # noqa: ANN001
    _validate_analytics_window(bucket, NOW - window, NOW)


@pytest.mark.parametrize(
    ("bucket", "window"),
    [
        ("minute", timedelta(hours=24, seconds=1)),
        ("hour", timedelta(days=31, seconds=1)),
        ("day", timedelta(days=31, seconds=1)),
    ],
)
def test_timeline_rejects_unbounded_queries(bucket, window):  # noqa: ANN001
    with pytest.raises(HTTPException) as exc:
        _validate_analytics_window(bucket, NOW - window, NOW)
    assert exc.value.status_code == 422
    assert "maximum" in exc.value.detail


def test_timeline_rejects_unknown_bucket():
    with pytest.raises(HTTPException) as exc:
        _validate_analytics_window("week", NOW - timedelta(days=1), NOW)
    assert exc.value.status_code == 422
    assert exc.value.detail == "Unsupported analytics bucket"


def test_timeline_rejects_naive_timestamps():
    with pytest.raises(HTTPException) as exc:
        _validate_analytics_window(
            "hour",
            datetime(2026, 8, 30, 10, 0),
            datetime(2026, 8, 30, 12, 0),
        )
    assert exc.value.status_code == 422
    assert "timezone" in exc.value.detail


@pytest.mark.parametrize("from_ts", [NOW, NOW + timedelta(seconds=1)])
def test_timeline_requires_a_forward_window(from_ts):  # noqa: ANN001
    with pytest.raises(HTTPException) as exc:
        _validate_analytics_window("minute", from_ts, NOW)
    assert exc.value.status_code == 422
    assert exc.value.detail == "from_ts must be earlier than to_ts"

