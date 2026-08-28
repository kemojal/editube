"""Structural verification of a committed harness run.

Fast checks that run inside the apply job, against the committed payload —
the §20.1 set that needs no rendering. Each check reports
`pass | warn | fail`; any `fail` marks the run failed with revert offered.
Render-level verification (frame sampling, loudness) attaches here later —
the report shape already carries it.
"""

from __future__ import annotations

from typing import Any

from app.services.harness.schemas import HarnessPlan, entity_id


def _check(name: str, status: str, detail: str | None = None) -> dict[str, Any]:
    entry = {"check": name, "status": status}
    if detail:
        entry["detail"] = detail
    return entry


def verify_committed(
    payload: dict[str, Any], run: Any, plan: HarnessPlan
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    items = [i for i in (payload.get("timelineMediaItems") or []) if isinstance(i, dict)]
    overlays = [o for o in (payload.get("textOverlays") or []) if isinstance(o, dict)]
    tracks = [t for t in (payload.get("timelineTracks") or []) if isinstance(t, dict)]
    attrs = payload.get("clipAttributes") or {}

    item_ids = [str(i.get("id")) for i in items]
    if len(set(item_ids)) != len(item_ids):
        checks.append(_check("unique_item_ids", "fail", "Duplicate timeline item ids."))
    else:
        checks.append(_check("unique_item_ids", "pass"))

    track_ids = {str(t.get("id")) for t in tracks}
    orphans = [i for i in items if i.get("trackId") and str(i.get("trackId")) not in track_ids]
    checks.append(
        _check(
            "items_reference_tracks",
            "fail" if orphans else "pass",
            f"{len(orphans)} item(s) reference a missing track." if orphans else None,
        )
    )

    manifest = run.applied_manifest or {}
    for item_id in manifest.get("timelineMediaItemIds") or []:
        present = any(str(i.get("id")) == item_id for i in items)
        checks.append(
            _check(
                f"created_item:{item_id}",
                "pass" if present else "fail",
                None if present else "Created item is missing from the committed draft.",
            )
        )
    for overlay_id in manifest.get("textOverlayIds") or []:
        present = any(str(o.get("id")) == overlay_id for o in overlays)
        checks.append(
            _check(
                f"created_overlay:{overlay_id}",
                "pass" if present else "fail",
                None if present else "Created overlay is missing from the committed draft.",
            )
        )

    # Audio ownership: a linked duplicate must not own audio (plan §7.1).
    for op in plan.operations:
        if op.type != "timeline.duplicate_linked" or not op.enabled:
            continue
        item_id = entity_id(run.id, op.id)
        item = next((i for i in items if str(i.get("id")) == item_id), None)
        if item is None:
            continue
        if item.get("audioEnabled"):
            checks.append(
                _check(
                    f"audio_ownership:{item_id}",
                    "fail",
                    "A linked visual duplicate came out with audio enabled.",
                )
            )
        else:
            checks.append(_check(f"audio_ownership:{item_id}", "pass"))

    # Staged mask assets must be present and resolvable.
    for op in plan.operations:
        if op.type != "visual.apply_subject_mask" or not op.enabled:
            continue
        clip_key = f"media:{entity_id(run.id, op.targetOp)}"
        processing = ((attrs.get(clip_key) or {}).get("processing") or {}).get("remove_bg") or {}
        status = str(processing.get("status") or "")
        url = str(processing.get("outputUrl") or "")
        if status == "completed" and url:
            checks.append(_check(f"mask_asset:{clip_key}", "pass"))
        elif getattr(op, "required", True):
            checks.append(
                _check(
                    f"mask_asset:{clip_key}",
                    "fail",
                    "The subject mask did not complete; the composite would export unmasked.",
                )
            )
        else:
            checks.append(
                _check(f"mask_asset:{clip_key}", "warn", "Optional mask missing.")
            )

    # Overlay geometry sanity.
    for overlay in overlays:
        oid = str(overlay.get("id"))
        start = overlay.get("start")
        end = overlay.get("end")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or end <= start:
            checks.append(_check(f"overlay_range:{oid}", "fail", "Overlay has no valid time range."))
        x = overlay.get("x")
        y = overlay.get("y")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            if not (0 <= float(x) <= 1 and 0 <= float(y) <= 1):
                checks.append(
                    _check(f"overlay_bounds:{oid}", "warn", "Overlay anchor is outside the frame.")
                )

    statuses = {c["status"] for c in checks}
    overall = "fail" if "fail" in statuses else ("warnings" if "warn" in statuses else "pass")
    return {"status": overall, "checks": checks}
