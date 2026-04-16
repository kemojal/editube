from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os

from sqlalchemy.orm import Session

from app.db.models import Project, ProjectArchiveState, ProjectRetentionPolicy
from app.jobs.queue import enqueue_archive_cold_storage_job


def is_project_due_for_archive(created_at: datetime | None, archive_after_days: int, now: datetime | None = None) -> bool:
    if not created_at:
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    baseline = now or datetime.now(timezone.utc)
    if baseline.tzinfo is None:
        baseline = baseline.replace(tzinfo=timezone.utc)
    return created_at <= baseline - timedelta(days=max(1, int(archive_after_days)))


def enqueue_due_retention_projects(db: Session) -> int:
    if os.getenv("COLD_STORAGE_ENABLED", "0").strip().lower() in {"0", "false", "off"}:
        return 0
    now = datetime.now(timezone.utc)
    policies = (
        db.query(ProjectRetentionPolicy)
        .filter(ProjectRetentionPolicy.auto_archive_enabled == True)  # noqa: E712
        .all()
    )
    if not policies:
        projects = db.query(Project.id, Project.created_at).all()
        count = 0
        for pid, created_at in projects:
            created = created_at
            if created and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if not created:
                continue
            if is_project_due_for_archive(created, 90, now=now):
                if enqueue_archive_cold_storage_job(pid):
                    count += 1
        return count

    count = 0
    for policy in policies:
        project = db.query(Project).filter(Project.id == policy.project_id).first()
        if not project:
            continue
        created = project.created_at
        if created and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if not created:
            continue
        if not is_project_due_for_archive(created, int(policy.archive_after_days or 90), now=now):
            continue
        state = db.query(ProjectArchiveState).filter(ProjectArchiveState.project_id == project.id).first()
        if state and state.state == "cold_storage":
            continue
        if enqueue_archive_cold_storage_job(project.id):
            count += 1
    return count
