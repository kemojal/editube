"""Editube RQ worker with Sentry and provider-neutral job lifecycle metrics."""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone

from redis import Redis
from rq import Queue, SimpleWorker

from app.services.observability import capture_message, init_sentry, set_observability_tags
from app.services.job_terminal_state import feature_key_for_job, persisted_job_context
from app.services.product_analytics import emit_after_commit


logger = logging.getLogger(__name__)

# These jobs persist their own domain-aware feature terminal event in the same
# transaction as the result. The generic worker still owns job_* events, but
# must not double-count feature completion/failure for them.
_SELF_INSTRUMENTED_FEATURE_TERMINALS = (
    "transcri",
    "ugc_render",
    "aspect_export",
    "multi_format_export",
    "delivery_package",
    "youtube_publish",
    "drive_import",
)


def _worker_owns_feature_terminal(job_type: str) -> bool:
    lowered = job_type.lower()
    return not any(fragment in lowered for fragment in _SELF_INSTRUMENTED_FEATURE_TERMINALS)

def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _job_context(job) -> dict:  # noqa: ANN001
    name = str(getattr(job, "func_name", "unknown")).split(".")[-1]
    args = getattr(job, "args", None) or [None]
    first_arg = args[0]
    resource = persisted_job_context(
        name,
        first_arg if isinstance(first_arg, (int, str)) else None,
    )
    requested_feature = (
        args[1]
        if "ugc_render" in name.lower()
        and len(args) > 1
        and args[1] in {"ugc_render", "ugc_regenerate"}
        else None
    )
    return {
        "job_id": str(job.id),
        "job_type": name[:120],
        "queue": str(getattr(job, "origin", "default"))[:80],
        "feature_key": requested_feature or feature_key_for_job(name, resource),
        "resource_id": first_arg if isinstance(first_arg, (int, str)) else None,
        "project_id": resource.get("project_id"),
        "video_id": resource.get("video_id"),
        "user_id": resource.get("user_id"),
        "workspace_id": resource.get("workspace_id"),
        "terminal_status": resource.get("status"),
    }


def _split_job_context(context: dict) -> tuple[dict, dict, str | None]:
    """Separate event properties from outbox identity and internal state."""

    properties = dict(context)
    attribution = {
        "user_id": properties.pop("user_id", None),
        "workspace_id": properties.pop("workspace_id", None),
    }
    terminal_status = properties.pop("terminal_status", None)
    return properties, attribution, terminal_status


def _is_analytics_maintenance_job(job) -> bool:  # noqa: ANN001
    func_name = str(getattr(job, "func_name", ""))
    return "analytics_delivery" in func_name or "analytics_privacy" in func_name


def _returned_job_status(job) -> str | None:  # noqa: ANN001
    try:
        getter = getattr(job, "return_value", None)
        result = getter() if callable(getter) else getattr(job, "result", None)
    except Exception:
        result = getattr(job, "result", None)
    if isinstance(result, dict):
        status = str(result.get("status") or "").strip().lower()
        return status or None
    return None


class EditubeWorker(SimpleWorker):
    def prepare_job_execution(self, job, remove_from_intermediate_queue: bool = False):  # noqa: ANN001
        super().prepare_job_execution(job, remove_from_intermediate_queue)
        if _is_analytics_maintenance_job(job):
            return
        now = datetime.utcnow()
        job.meta["analytics_started_at"] = now.isoformat()
        job.meta["analytics_attempt"] = int(job.meta.get("analytics_attempt", 0)) + 1
        job.save_meta()
        context, attribution, _ = _split_job_context(_job_context(job))
        set_observability_tags(
            job_id=context["job_id"],
            job_type=context["job_type"],
            queue=context["queue"],
            feature_key=context.get("feature_key"),
            job_attempt=job.meta["analytics_attempt"],
        )
        enqueued_at = _naive_utc(getattr(job, "enqueued_at", None))
        queue_wait_ms = int((now - enqueued_at).total_seconds() * 1000) if enqueued_at else None
        emit_after_commit(
            "job_started",
            source="worker",
            **attribution,
            properties={
                **context,
                "attempt_number": job.meta["analytics_attempt"],
                "queue_wait_ms": max(0, queue_wait_ms) if queue_wait_ms is not None else None,
            },
            event_id=f"rq:{job.id}:{job.meta['analytics_attempt']}:started",
        )
        if job.meta["analytics_attempt"] > 1:
            emit_after_commit(
                "job_retried",
                source="worker",
                **attribution,
                properties={
                    **context,
                    "attempt_number": job.meta["analytics_attempt"],
                    "failure_class": "processing",
                },
                event_id=f"rq:{job.id}:{job.meta['analytics_attempt']}:retried",
            )

    def handle_job_success(self, job, queue, started_job_registry):  # noqa: ANN001
        if _is_analytics_maintenance_job(job):
            return super().handle_job_success(job, queue, started_job_registry)
        started_raw = job.meta.get("analytics_started_at")
        started = datetime.fromisoformat(started_raw) if started_raw else None
        duration_ms = int((datetime.utcnow() - started).total_seconds() * 1000) if started else None
        context, attribution, persisted_status = _split_job_context(_job_context(job))
        terminal_status = _returned_job_status(job) or persisted_status
        completed_properties = {
            **context,
            "attempt_number": int(job.meta.get("analytics_attempt", 1)),
            "duration_ms": max(0, duration_ms) if duration_ms is not None else None,
            "result": "success",
        }
        if terminal_status in {"failed", "failure", "error"}:
            failed_properties = {
                **completed_properties,
                "result": "failure",
                "failure_class": "persisted_failure",
                "error_code": "job_returned_failed",
            }
            emit_after_commit(
                "job_failed",
                source="worker",
                **attribution,
                properties=failed_properties,
                event_id=f"rq:{job.id}:{completed_properties['attempt_number']}:persisted-failed",
            )
            if context.get("feature_key") and _worker_owns_feature_terminal(context["job_type"]):
                emit_after_commit(
                    "feature_failed",
                    source="worker",
                    **attribution,
                    properties=failed_properties,
                    event_id=(
                        f"rq:{job.id}:{completed_properties['attempt_number']}:"
                        "feature-persisted-failed"
                    ),
                )
            capture_message(
                "RQ job returned after persisting a failed state",
                level="error",
                job_type=context["job_type"],
                job_id=context["job_id"],
                feature_key=str(context.get("feature_key") or "unknown"),
            )
            return super().handle_job_success(job, queue, started_job_registry)
        if terminal_status in {"canceled", "cancelled"}:
            canceled_properties = {**completed_properties, "result": "canceled"}
            emit_after_commit(
                "job_canceled",
                source="worker",
                **attribution,
                properties=canceled_properties,
                event_id=f"rq:{job.id}:{completed_properties['attempt_number']}:canceled",
            )
            return super().handle_job_success(job, queue, started_job_registry)
        emit_after_commit(
            "job_completed",
            source="worker",
            **attribution,
            properties=completed_properties,
            event_id=f"rq:{job.id}:{completed_properties['attempt_number']}:completed",
        )
        if context.get("feature_key") and _worker_owns_feature_terminal(context["job_type"]):
            emit_after_commit(
                "feature_completed",
                source="worker",
                **attribution,
                properties={**completed_properties, "completion_type": "background_job"},
                event_id=f"rq:{job.id}:{completed_properties['attempt_number']}:feature-completed",
            )
        return super().handle_job_success(job, queue, started_job_registry)

    def handle_job_failure(self, job, queue, started_job_registry=None, exc_string=""):  # noqa: ANN001, ARG002
        if _is_analytics_maintenance_job(job):
            return super().handle_job_failure(job, queue, started_job_registry, exc_string)
        started_raw = job.meta.get("analytics_started_at")
        started = datetime.fromisoformat(started_raw) if started_raw else None
        duration_ms = int((datetime.utcnow() - started).total_seconds() * 1000) if started else None
        context, attribution, _ = _split_job_context(_job_context(job))
        failed_properties = {
            **context,
            "attempt_number": int(job.meta.get("analytics_attempt", 1)),
            "duration_ms": max(0, duration_ms) if duration_ms is not None else None,
            "result": "failure",
            "failure_class": "processing",
            "error_code": "job_exception",
        }
        emit_after_commit(
            "job_failed",
            source="worker",
            **attribution,
            properties=failed_properties,
            event_id=f"rq:{job.id}:{failed_properties['attempt_number']}:failed",
        )
        if context.get("feature_key") and _worker_owns_feature_terminal(context["job_type"]):
            emit_after_commit(
                "feature_failed",
                source="worker",
                **attribution,
                properties=failed_properties,
                event_id=f"rq:{job.id}:{failed_properties['attempt_number']}:feature-failed",
            )
        return super().handle_job_failure(job, queue, started_job_registry, exc_string)


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    init_sentry("worker")
    redis_url = (os.getenv("REDIS_URL") or "").strip()
    if not redis_url:
        raise RuntimeError("REDIS_URL is required")
    queue_names = sys.argv[1:] or ["default"]
    connection = Redis.from_url(redis_url)
    queues = [Queue(name, connection=connection) for name in queue_names]
    worker = EditubeWorker(queues, connection=connection)
    return 0 if worker.work(with_scheduler=False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
