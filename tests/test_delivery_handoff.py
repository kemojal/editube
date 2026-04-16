from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.jobs.multi_format_export import _PROFILES
from app.services.retention import is_project_due_for_archive


def test_multi_format_profiles_include_required_targets() -> None:
    assert {"4k_master", "yt_1080p", "social_720p"}.issubset(set(_PROFILES.keys()))


def test_project_due_for_archive_uses_threshold_days() -> None:
    now = datetime(2026, 4, 15, tzinfo=timezone.utc)
    created_old = now - timedelta(days=120)
    created_new = now - timedelta(days=30)

    assert is_project_due_for_archive(created_old, archive_after_days=90, now=now) is True
    assert is_project_due_for_archive(created_new, archive_after_days=90, now=now) is False
