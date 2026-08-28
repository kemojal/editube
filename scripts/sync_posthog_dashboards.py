#!/usr/bin/env python3
"""Validate or idempotently sync the managed PostHog dashboards.

Dry-run validation is the default. `--apply` requires a personal API key with
dashboard:read/write and insight:read/write. It never sends sample events.
"""

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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_MANIFEST = ROOT / "config" / "analytics" / "posthog-dashboards.json"
KNOWN_CLIENT_EVENTS = {
    "page_viewed", "page_left", "section_viewed", "cta_clicked", "form_started",
    "form_submitted", "form_failed", "form_abandoned", "feature_exposed", "feature_opened",
    "feature_started", "feature_completed", "feature_failed", "feature_canceled",
    "feature_result_used", "navigation_clicked", "landing_cta_clicked", "landing_feature_viewed",
    "pricing_viewed", "pricing_interval_changed", "faq_opened", "guide_section_viewed",
    "help_searched", "community_category_viewed", "roadmap_filtered", "roadmap_vote_attempted",
    "contact_clicked", "signup_viewed", "signup_method_selected", "signup_submitted",
    "signup_failed", "login_submitted", "onboarding_started", "onboarding_step_viewed",
    "onboarding_step_failed", "onboarding_step_skipped", "onboarding_step_back_clicked",
    "onboarding_workflow_selected", "checkout_clicked", "checkout_failed", "checkout_returned",
    "checkout_canceled", "checkout_return_failed", "onboarding_plan_selected",
    "onboarding_billing_interval_changed", "invoice_viewed", "upgrade_prompt_viewed",
    "upgrade_prompt_clicked", "project_wizard_opened", "project_wizard_step_viewed",
    "project_wizard_step_skipped", "project_source_selected", "upload_started", "upload_failed",
    "project_range_selected", "project_tool_selected", "project_create_submitted",
    "notification_clicked", "signup_acquisition_claimed",
    "signup_acquisition_claim_failed", "pricing_plan_selected", "affiliate_cta_clicked",
    "media_playback_failed", "client_error_observed", "websocket_disconnected",
    "editor_session_ended",
}
REQUIRED_INSIGHT_FIELDS = {
    "key", "name", "kind", "denominator", "definition", "freshness", "limitations",
}


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    dashboards = manifest.get("dashboards")
    if manifest.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if not isinstance(dashboards, list) or not dashboards:
        return errors + ["dashboards must be a non-empty array"]

    try:
        from app.services.analytics_events import SERVER_EVENT_NAMES
    except ImportError:
        SERVER_EVENT_NAMES = frozenset()
    known_events = set(SERVER_EVENT_NAMES) | KNOWN_CLIENT_EVENTS
    dashboard_keys: set[str] = set()
    managed_names: set[str] = set()
    for dashboard in dashboards:
        key = dashboard.get("key")
        if not key or key in dashboard_keys:
            errors.append(f"duplicate or missing dashboard key: {key!r}")
        dashboard_keys.add(key)
        for field in ("name", "owner", "review_cadence", "definition", "freshness", "limitations"):
            if not str(dashboard.get(field) or "").strip():
                errors.append(f"dashboard {key!r} missing {field}")
        insights = dashboard.get("insights")
        if not isinstance(insights, list) or not insights:
            errors.append(f"dashboard {key!r} has no insights")
            continue
        insight_keys: set[str] = set()
        for insight in insights:
            missing = REQUIRED_INSIGHT_FIELDS - set(insight)
            if missing:
                errors.append(f"insight {key}/{insight.get('key')} missing {sorted(missing)}")
            insight_key = insight.get("key")
            if not insight_key or insight_key in insight_keys:
                errors.append(f"duplicate or missing insight key in {key}: {insight_key!r}")
            insight_keys.add(insight_key)
            managed_name = managed_insight_name(key, insight_key, insight.get("name", ""))
            if managed_name in managed_names:
                errors.append(f"duplicate managed insight name: {managed_name}")
            managed_names.add(managed_name)
            kind = insight.get("kind")
            if kind not in {"trend", "funnel", "retention"}:
                errors.append(f"insight {key}/{insight_key} has unsupported kind {kind!r}")
            events = [item.get("event") for item in insight.get("series", [])]
            if kind == "retention":
                events.extend([insight.get("target_event"), insight.get("returning_event")])
            for event in events:
                if event not in known_events:
                    errors.append(f"insight {key}/{insight_key} uses unknown event {event!r}")
    return errors


def managed_insight_name(dashboard_key: str, insight_key: str, title: str) -> str:
    return f"[Editube managed] {dashboard_key}/{insight_key} — {title}"[:400]


def _property_filter(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": item["key"],
        "value": item["value"],
        "operator": item.get("operator", "exact"),
        "type": "event",
    }


def _event_node(item: dict[str, Any], group_index: int | None) -> dict[str, Any]:
    node: dict[str, Any] = {
        "kind": "EventsNode",
        "event": item["event"],
        "custom_name": item.get("name"),
        "properties": [_property_filter(value) for value in item.get("filters", [])],
    }
    if group_index is not None:
        node["math"] = "unique_group"
        node["math_group_type_index"] = group_index
    return node


def _uses_workspace_denominator(insight: dict[str, Any]) -> bool:
    return "workspace" in str(insight.get("denominator", "")).lower()


def build_query(insight: dict[str, Any], workspace_group_index: int | None) -> dict[str, Any]:
    group_index = workspace_group_index if _uses_workspace_denominator(insight) else None
    common: dict[str, Any] = {
        "dateRange": {"date_from": insight.get("date_from", "-30d")},
        "filterTestAccounts": True,
    }
    if group_index is not None:
        common["aggregation_group_type_index"] = group_index
    breakdown = insight.get("breakdown")
    if breakdown:
        common["breakdownFilter"] = {
            "breakdown": breakdown,
            "breakdown_type": "event",
            "breakdown_limit": 25,
        }

    if insight["kind"] == "retention":
        source = {
            "kind": "RetentionQuery",
            **common,
            "retentionFilter": {
                "targetEntity": {"id": insight["target_event"], "type": "events"},
                "returningEntity": {"id": insight["returning_event"], "type": "events"},
                "period": insight.get("period", "Week"),
                "totalIntervals": int(insight.get("intervals", 8)),
                "retentionType": "retention_first_time",
            },
        }
    else:
        source = {
            "kind": "FunnelsQuery" if insight["kind"] == "funnel" else "TrendsQuery",
            **common,
            "series": [_event_node(item, group_index) for item in insight["series"]],
        }
        if insight["kind"] == "funnel":
            source["funnelsFilter"] = {
                "funnelOrderType": "ordered",
                "funnelWindowInterval": int(insight.get("window_days", 14)),
                "funnelWindowIntervalUnit": "day",
                "hideIncompleteConversionWindowPeriods": True,
            }
    return {"kind": "InsightVizNode", "source": source}


def _insight_description(insight: dict[str, Any]) -> str:
    value = (
        f"Definition: {insight['definition']} Denominator: {insight['denominator']} "
        f"Freshness: {insight['freshness']} Limitation: {insight['limitations']}"
    )
    return value[:400]


class PostHogApi:
    def __init__(self, host: str, project_id: str, token: str):
        self.base = f"{host.rstrip('/')}/api/projects/{quote(project_id, safe='')}"
        self.token = token

    def request(self, method: str, path: str, payload: dict | None = None) -> Any:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            f"{self.base}{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
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
                    f"PostHog API {method} {path} failed ({exc.code}): {detail}"
                ) from exc
            except (TimeoutError, URLError) as exc:
                if retryable_method and attempt < 2:
                    time.sleep(2**attempt)
                    continue
                raise RuntimeError(f"PostHog API {method} {path} failed: {exc}") from exc
        raise AssertionError("unreachable")

    def find(self, resource: str, name: str) -> dict | None:
        return next((item for item in self.search(resource, name) if item.get("name") == name), None)

    def search(self, resource: str, value: str) -> list[dict]:
        query = urlencode({"search": value, "limit": 100, "include_dashboards": "true"})
        result = self.request("GET", f"/{resource}/?{query}") or {}
        return list(result.get("results", []))

    def managed_insights(self, dashboard_key: str, insight_key: str) -> list[dict]:
        prefix = f"[Editube managed] {dashboard_key}/{insight_key} — "
        return [
            item
            for item in self.search("insights", prefix)
            if str(item.get("name") or "").startswith(prefix)
        ]


def sync(manifest: dict[str, Any], api: PostHogApi, workspace_group_index: int | None) -> dict[str, int]:
    counts = {
        "dashboards_created": 0,
        "dashboards_updated": 0,
        "insights_created": 0,
        "insights_updated": 0,
        "duplicate_insights_archived": 0,
    }
    tag = manifest["managed_tag"]
    for dashboard in manifest["dashboards"]:
        dashboard_payload = {
            "name": dashboard["name"],
            "description": (
                f"{dashboard['definition']} Owner: {dashboard['owner']}. "
                f"Freshness: {dashboard['freshness']} Limitations: {dashboard['limitations']}"
            ),
            "tags": [tag, dashboard["owner"]],
            "pinned": False,
        }
        existing_dashboard = api.find("dashboards", dashboard["name"])
        if existing_dashboard:
            dashboard_row = api.request(
                "PATCH", f"/dashboards/{existing_dashboard['id']}/", dashboard_payload
            )
            counts["dashboards_updated"] += 1
        else:
            dashboard_row = api.request("POST", "/dashboards/", dashboard_payload)
            counts["dashboards_created"] += 1
        dashboard_id = int(dashboard_row["id"])

        for insight in dashboard["insights"]:
            name = managed_insight_name(dashboard["key"], insight["key"], insight["name"])
            payload = {
                "name": name,
                "description": _insight_description(insight),
                "query": build_query(insight, workspace_group_index),
                "dashboards": [dashboard_id],
                "tags": [tag, dashboard["key"]],
                "favorited": False,
            }
            managed_matches = api.managed_insights(dashboard["key"], insight["key"])
            existing_insight = next(
                (item for item in managed_matches if item.get("name") == name),
                min(managed_matches, key=lambda item: int(item["id"])) if managed_matches else None,
            )
            if existing_insight:
                api.request("PATCH", f"/insights/{existing_insight['id']}/?include_dashboards=true", payload)
                counts["insights_updated"] += 1
            else:
                existing_insight = api.request("POST", "/insights/?include_dashboards=true", payload)
                counts["insights_created"] += 1
            keep_id = int(existing_insight["id"])
            for duplicate in managed_matches:
                if int(duplicate["id"]) == keep_id:
                    continue
                # PostHog intentionally rejects hard DELETE for insights. Its
                # documented removal contract is a reversible soft delete.
                api.request("PATCH", f"/insights/{duplicate['id']}/", {"deleted": True})
                counts["duplicate_insights_archived"] += 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--apply", action="store_true", help="Write managed dashboards to PostHog")
    args = parser.parse_args()
    manifest = load_manifest(args.manifest)
    errors = validate_manifest(manifest)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    workspace_group_raw = os.getenv("POSTHOG_WORKSPACE_GROUP_TYPE_INDEX", "").strip()
    workspace_group_index = int(workspace_group_raw) if workspace_group_raw else None
    workspace_insights = sum(
        _uses_workspace_denominator(insight)
        for dashboard in manifest["dashboards"]
        for insight in dashboard["insights"]
    )
    if not args.apply:
        print(
            json.dumps(
                {
                    "valid": True,
                    "dashboards": len(manifest["dashboards"]),
                    "insights": sum(len(item["insights"]) for item in manifest["dashboards"]),
                    "workspace_group_index_required_for_apply": workspace_insights > 0,
                },
                indent=2,
            )
        )
        return 0
    if workspace_insights and workspace_group_index is None:
        print("ERROR: POSTHOG_WORKSPACE_GROUP_TYPE_INDEX is required for workspace denominators", file=sys.stderr)
        return 2

    project_id = os.getenv("POSTHOG_PROJECT_ID", "").strip()
    token = os.getenv("POSTHOG_PERSONAL_API_KEY", "").strip()
    host = os.getenv("POSTHOG_API_HOST", "https://us.posthog.com").strip()
    if not project_id or not token:
        print("ERROR: POSTHOG_PROJECT_ID and POSTHOG_PERSONAL_API_KEY are required", file=sys.stderr)
        return 2
    result = sync(manifest, PostHogApi(host, project_id, token), workspace_group_index)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
