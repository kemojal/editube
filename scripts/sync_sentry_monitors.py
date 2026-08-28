#!/usr/bin/env python3
"""Validate or idempotently sync Sentry reliability monitors and workflow."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "config" / "analytics" / "sentry-monitors.json"


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if manifest.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if not manifest.get("environment"):
        errors.append("environment is required")
    monitors = manifest.get("monitors")
    if not isinstance(monitors, list) or not monitors:
        return errors + ["monitors must be a non-empty array"]
    keys: set[str] = set()
    names: set[str] = set()
    for monitor in monitors:
        for field in (
            "key", "name", "project_env", "description", "aggregate", "dataset",
            "event_types", "query", "time_window_seconds", "critical_above",
            "resolve_at_or_below",
        ):
            if field not in monitor:
                errors.append(f"monitor {monitor.get('key')!r} missing {field}")
        if monitor.get("key") in keys:
            errors.append(f"duplicate monitor key {monitor.get('key')!r}")
        if monitor.get("name") in names:
            errors.append(f"duplicate monitor name {monitor.get('name')!r}")
        keys.add(monitor.get("key"))
        names.add(monitor.get("name"))
        if int(monitor.get("resolve_at_or_below", 0)) >= int(monitor.get("critical_above", 0)):
            errors.append(f"monitor {monitor.get('key')!r} resolve threshold must be lower")
    workflow = manifest.get("workflow") or {}
    for field in ("name", "frequency_minutes", "trigger_types", "action"):
        if not workflow.get(field):
            errors.append(f"workflow missing {field}")
    return errors


class SentryApi:
    def __init__(self, host: str, organization: str, token: str):
        self.base = f"{host.rstrip('/')}/api/0/organizations/{quote(organization, safe='')}"
        self.token = token

    def request(self, method: str, path: str, payload: dict | None = None) -> Any:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            f"{self.base}{path}",
            data=body,
            method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        retryable_method = method in {"GET", "PUT", "PATCH", "DELETE"}
        for attempt in range(3):
            try:
                with urlopen(request, timeout=30) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else None
            except HTTPError as exc:
                detail = exc.read(1000).decode("utf-8", errors="replace")
                if retryable_method and exc.code in {429, 502, 503, 504} and attempt < 2:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                    time.sleep(min(delay, 10))
                    continue
                raise RuntimeError(
                    f"Sentry API {method} {path} failed ({exc.code}): {detail}"
                ) from exc
            except (TimeoutError, URLError) as exc:
                if retryable_method and attempt < 2:
                    time.sleep(2**attempt)
                    continue
                raise RuntimeError(f"Sentry API {method} {path} failed: {exc}") from exc
        raise AssertionError("unreachable")

    def list_detectors(self, project: str) -> list[dict[str, Any]]:
        result = self.request(
            "GET",
            f"/detectors/?{urlencode({'project': project})}",
        ) or []
        return result.get("results", []) if isinstance(result, dict) else result

    def list_workflows(self, name: str) -> list[dict[str, Any]]:
        result = self.request("GET", f"/workflows/?{urlencode({'query': name})}") or []
        return result.get("results", []) if isinstance(result, dict) else result

    def create_detector(self, project: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/projects/{quote(project, safe='')}/detectors/",
            payload,
        )

    def update_detector(self, detector_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("PUT", f"/detectors/{detector_id}/", payload)


def monitor_payload(monitor: dict[str, Any], manifest: dict[str, Any], owner: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": monitor["name"],
        "description": monitor["description"],
        "type": "metric_issue",
        "enabled": True,
        "workflow_ids": [],
        "data_sources": [
            {
                "aggregate": monitor["aggregate"],
                "dataset": monitor["dataset"],
                "environment": manifest["environment"],
                "eventTypes": monitor["event_types"],
                "query": monitor["query"],
                "queryType": 0,
                "timeWindow": int(monitor["time_window_seconds"]),
            }
        ],
        "config": {"detectionType": "static"},
        "condition_group": {
            "logicType": "any",
            "conditions": [
                {
                    "type": "gt",
                    "comparison": int(monitor["critical_above"]),
                    "conditionResult": 75,
                },
                {
                    "type": "lte",
                    "comparison": int(monitor["resolve_at_or_below"]),
                    "conditionResult": 0,
                },
            ],
            "actions": [],
        },
    }
    if owner:
        payload["owner"] = owner
    return payload


def workflow_payload(
    workflow: dict[str, Any],
    detector_ids: list[int],
    environment: str,
    owner: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": workflow["name"],
        "enabled": True,
        "detector_ids": detector_ids,
        "environment": environment,
        "config": {"frequency": int(workflow["frequency_minutes"])},
        "triggers": {
            "logicType": "any-short",
            "conditions": [
                {"type": trigger, "comparison": True, "conditionResult": True}
                for trigger in workflow["trigger_types"]
            ],
            "actions": [],
        },
        "action_filters": [
            {
                "logicType": "all",
                "conditions": [],
                "actions": [
                    {
                        "type": "email",
                        "integrationId": None,
                        "data": {"fallthroughType": "ActiveMembers"},
                        "config": {
                            "targetType": "issue_owners",
                            "targetDisplay": None,
                            "targetIdentifier": None,
                        },
                        "status": "active",
                    }
                ],
            }
        ],
    }
    if owner:
        payload["owner"] = owner
    return payload


def sync(manifest: dict[str, Any], api: SentryApi, owner: str | None) -> dict[str, int]:
    counts = {"monitors_created": 0, "monitors_updated": 0, "workflows_created": 0, "workflows_updated": 0}
    detector_ids: list[int] = []
    for monitor in manifest["monitors"]:
        project = os.getenv(monitor["project_env"], "").strip()
        if not project:
            raise RuntimeError(f"{monitor['project_env']} is required")
        existing = next(
            (row for row in api.list_detectors(project) if row.get("name") == monitor["name"]),
            None,
        )
        payload = monitor_payload(monitor, manifest, owner)
        if existing:
            result = api.update_detector(int(existing["id"]), payload)
            counts["monitors_updated"] += 1
        else:
            result = api.create_detector(project, payload)
            counts["monitors_created"] += 1
        detector_ids.append(int(result["id"]))

    workflow = manifest["workflow"]
    existing_workflow = next(
        (row for row in api.list_workflows(workflow["name"]) if row.get("name") == workflow["name"]),
        None,
    )
    payload = workflow_payload(workflow, detector_ids, manifest["environment"], owner)
    if existing_workflow:
        api.request("PUT", f"/workflows/{existing_workflow['id']}/", payload)
        counts["workflows_updated"] += 1
    else:
        api.request("POST", "/workflows/", payload)
        counts["workflows_created"] += 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    manifest = load_manifest(args.manifest)
    errors = validate_manifest(manifest)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    if not args.apply:
        print(json.dumps({"valid": True, "monitors": len(manifest["monitors"]), "workflows": 1}, indent=2))
        return 0

    organization = os.getenv("SENTRY_ORG", "").strip()
    token = os.getenv("SENTRY_AUTH_TOKEN", "").strip()
    owner = os.getenv("SENTRY_ALERT_OWNER", "").strip() or None
    host = os.getenv("SENTRY_API_HOST", "https://sentry.io").strip()
    if not organization or not token:
        print("ERROR: SENTRY_ORG and SENTRY_AUTH_TOKEN are required", file=sys.stderr)
        return 2
    result = sync(manifest, SentryApi(host, organization, token), owner)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
