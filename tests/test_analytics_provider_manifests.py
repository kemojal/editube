from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_posthog_dashboard_manifest_is_complete_and_queryable():
    module = _load_script("sync_posthog_dashboards")
    manifest = module.load_manifest()

    assert module.validate_manifest(manifest) == []
    assert len(manifest["dashboards"]) == 8
    assert sum(len(row["insights"]) for row in manifest["dashboards"]) == 25
    for dashboard in manifest["dashboards"]:
        for insight in dashboard["insights"]:
            query = module.build_query(insight, workspace_group_index=0)
            assert query["kind"] == "InsightVizNode"
            assert query["source"]["kind"] in {
                "TrendsQuery",
                "FunnelsQuery",
                "RetentionQuery",
            }
            assert len(module._insight_description(insight)) <= 400
            if "workspace" in insight["denominator"].lower():
                assert query["source"]["aggregation_group_type_index"] == 0


def test_posthog_sync_reuses_stable_insight_key_and_prunes_renamed_duplicate():
    module = _load_script("sync_posthog_dashboards")
    dashboard = {
        "key": "checkout",
        "name": "Checkout",
        "owner": "billing",
        "definition": "Checkout health.",
        "freshness": "5 minutes",
        "limitations": "Test data excluded.",
        "review_cadence": "weekly",
        "insights": [
            {
                "key": "losses",
                "name": "Checkout losses",
                "kind": "trend",
                "series": [{"event": "checkout_abandoned"}],
                "date_from": "-30d",
                "denominator": "Checkout attempts",
                "definition": "Mature losses.",
                "freshness": "5 minutes",
                "limitations": "Maturity window applies.",
            }
        ],
    }

    class FakeApi:
        def __init__(self):
            self.calls = []

        def find(self, resource, name):
            assert resource == "dashboards"
            return {"id": 10, "name": name}

        def managed_insights(self, dashboard_key, insight_key):
            assert (dashboard_key, insight_key) == ("checkout", "losses")
            return [
                {"id": 20, "name": "[Editube managed] checkout/losses — Old title"},
                {"id": 21, "name": "[Editube managed] checkout/losses — Duplicate title"},
            ]

        def request(self, method, path, payload=None):
            self.calls.append((method, path, payload))
            if path.startswith("/dashboards/"):
                return {"id": 10}
            if method == "PATCH" and path.startswith("/insights/"):
                return {"id": 20}
            return None

    api = FakeApi()
    result = module.sync(
        {"managed_tag": "editube-managed", "dashboards": [dashboard]},
        api,
        workspace_group_index=None,
    )

    assert result == {
        "dashboards_created": 0,
        "dashboards_updated": 1,
        "insights_created": 0,
        "insights_updated": 1,
        "duplicate_insights_archived": 1,
    }
    assert any(
        call == ("PATCH", "/insights/21/", {"deleted": True}) for call in api.calls
    )


def test_sentry_monitor_manifest_has_resolving_thresholds_and_safe_actions():
    module = _load_script("sync_sentry_monitors")
    manifest = module.load_manifest()

    assert module.validate_manifest(manifest) == []
    assert len(manifest["monitors"]) == 5
    payload = module.monitor_payload(manifest["monitors"][0], manifest, "team:42")
    assert payload["owner"] == "team:42"
    assert payload["data_sources"][0]["environment"] == "production"
    conditions = payload["condition_group"]["conditions"]
    assert conditions[0]["type"] == "gt"
    assert conditions[1]["type"] == "lte"

    workflow = module.workflow_payload(
        manifest["workflow"], [1, 2], "production", "team:42"
    )
    action = workflow["action_filters"][0]["actions"][0]
    assert workflow["detector_ids"] == [1, 2]
    assert action["config"]["targetType"] == "issue_owners"
    assert action["data"]["fallthroughType"] == "ActiveMembers"


def test_sentry_detector_listing_uses_supported_organization_endpoint(monkeypatch):
    module = _load_script("sync_sentry_monitors")
    api = module.SentryApi("https://sentry.io", "kilotech", "token")
    calls = []

    def fake_request(method, path, payload=None):
        calls.append((method, path, payload))
        return {"results": [{"id": 1, "name": "Monitor"}]}

    monkeypatch.setattr(api, "request", fake_request)

    assert api.list_detectors("editube frontend") == [{"id": 1, "name": "Monitor"}]
    assert calls == [("GET", "/detectors/?project=editube+frontend", None)]

    calls.clear()
    api.update_detector(42, {"name": "Monitor"})
    assert calls == [("PUT", "/detectors/42/", {"name": "Monitor"})]
