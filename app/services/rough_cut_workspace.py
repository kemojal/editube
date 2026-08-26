"""Canonical persistence for the editor's project-wide rough-cut workspace.

The editor used to save its entire timeline below whichever ``video_id`` was
present in the URL. Imported media is also represented by a ``Video`` row, so
opening an imported asset could create a second, partial project draft. The
next refresh then loaded only one of those rows and appeared to lose clips.

One project now has one draft owner: its newest non-asset source video. Legacy
asset-owned drafts are merged once into that canonical draft. A schema marker
prevents a deliberately deleted overlay from being resurrected on later reads.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import AiResult, Video

ROUGH_CUT_ASSET_DESCRIPTION = "rough cut asset"
ROUGH_CUT_DRAFT_TYPE = "rough_cut_draft"
WORKSPACE_PERSISTENCE_KEY = "workspacePersistence"
WORKSPACE_SCHEMA_VERSION = 2

# These collections are project layers, not properties of the imported asset
# whose URL happened to be open when they were saved.
_LAYER_LIST_KEYS = (
    "timelineMediaItems",
    "timelineTracks",
    "lowerThirds",
    "elementOverlays",
    "gridClips",
    "markers",
    "transcriptComments",
)
_LAYER_MAP_KEYS = (
    "trackState",
    "clipAttributes",
)
_LAYER_VALUE_KEYS = (
    "textOverlay",
    "selectedGridClipId",
    "selectedGridCellId",
    "selectedClipTarget",
)


def is_rough_cut_asset(video: Video) -> bool:
    return str(video.description or "").strip().lower() == ROUGH_CUT_ASSET_DESCRIPTION


def latest_project_source_video(db: Session, project_id: int) -> Video | None:
    """Return the newest canonical source, excluding editor-library assets."""

    normalized_description = func.lower(
        func.trim(func.coalesce(Video.description, ""))
    )
    return (
        db.query(Video)
        .filter(Video.project_id == project_id)
        .filter(normalized_description != ROUGH_CUT_ASSET_DESCRIPTION)
        .order_by(Video.updated_at.desc(), Video.id.desc())
        .first()
    )


def resolve_workspace_video(db: Session, requested_video: Video) -> Video:
    """Resolve any source/asset URL to the project's canonical draft owner."""

    return latest_project_source_video(db, requested_video.project_id) or requested_video


def _draft_row(db: Session, video_id: int) -> AiResult | None:
    return (
        db.query(AiResult)
        .filter(
            AiResult.video_id == video_id,
            AiResult.result_type == ROUGH_CUT_DRAFT_TYPE,
        )
        .first()
    )


def _item_identity(item: Any) -> tuple[str, str]:
    if isinstance(item, dict):
        for key in ("id", "trackId", "key"):
            value = item.get(key)
            if value not in (None, ""):
                return key, str(value)
    # Preserve anonymous legacy values without collapsing equal-looking entries.
    return "object", str(id(item))


def _merge_unique_items(current: Any, legacy: Any) -> list[Any]:
    merged: list[Any] = []
    seen: set[tuple[str, str]] = set()
    for candidate in (
        current if isinstance(current, list) else [],
        legacy if isinstance(legacy, list) else [],
    ):
        for item in candidate:
            identity = _item_identity(item)
            if identity in seen:
                continue
            seen.add(identity)
            merged.append(deepcopy(item))
    return merged


def _merge_legacy_layers(canonical: dict[str, Any], legacy: dict[str, Any]) -> None:
    for key in _LAYER_LIST_KEYS:
        if isinstance(legacy.get(key), list):
            canonical[key] = _merge_unique_items(canonical.get(key), legacy[key])

    for key in _LAYER_MAP_KEYS:
        legacy_map = legacy.get(key)
        if not isinstance(legacy_map, dict):
            continue
        # Canonical values win on collisions; legacy contributes missing clips.
        canonical_map = canonical.get(key)
        canonical[key] = {
            **deepcopy(legacy_map),
            **(deepcopy(canonical_map) if isinstance(canonical_map, dict) else {}),
        }

    for key in _LAYER_VALUE_KEYS:
        if canonical.get(key) in (None, "", {}, []) and legacy.get(key) not in (
            None,
            "",
            {},
            [],
        ):
            canonical[key] = deepcopy(legacy[key])


def _persistence_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = payload.get(WORKSPACE_PERSISTENCE_KEY)
    return deepcopy(metadata) if isinstance(metadata, dict) else {}


def get_workspace_draft(
    db: Session, requested_video: Video
) -> tuple[Video, AiResult | None]:
    """Return the canonical row, merging split legacy asset drafts exactly once."""

    workspace_video = resolve_workspace_video(db, requested_video)
    canonical_row = _draft_row(db, workspace_video.id)
    payload = (
        deepcopy(canonical_row.result_data)
        if canonical_row is not None and isinstance(canonical_row.result_data, dict)
        else {}
    )
    metadata = _persistence_metadata(payload)
    if int(metadata.get("schemaVersion") or 0) >= WORKSPACE_SCHEMA_VERSION:
        return workspace_video, canonical_row

    legacy_rows = (
        db.query(AiResult)
        .join(Video, Video.id == AiResult.video_id)
        .filter(
            Video.project_id == workspace_video.project_id,
            Video.id != workspace_video.id,
            func.lower(func.trim(func.coalesce(Video.description, "")))
            == ROUGH_CUT_ASSET_DESCRIPTION,
            AiResult.result_type == ROUGH_CUT_DRAFT_TYPE,
        )
        .order_by(AiResult.updated_at.asc(), AiResult.id.asc())
        .all()
    )

    migrated_ids: list[int] = []
    for legacy_row in legacy_rows:
        if not isinstance(legacy_row.result_data, dict):
            continue
        _merge_legacy_layers(payload, legacy_row.result_data)
        migrated_ids.append(legacy_row.video_id)

    # Do not manufacture a draft merely because a pristine project was opened.
    if canonical_row is None and not legacy_rows:
        return workspace_video, None

    metadata.update(
        {
            "schemaVersion": WORKSPACE_SCHEMA_VERSION,
            "legacyDraftVideoIds": sorted(set(migrated_ids)),
        }
    )
    payload[WORKSPACE_PERSISTENCE_KEY] = metadata

    if canonical_row is None:
        canonical_row = AiResult(
            video_id=workspace_video.id,
            result_type=ROUGH_CUT_DRAFT_TYPE,
        )
        db.add(canonical_row)
    canonical_row.status = "completed"
    canonical_row.error_message = None
    canonical_row.result_data = payload
    db.commit()
    db.refresh(canonical_row)
    return workspace_video, canonical_row


def prepare_workspace_save(
    db: Session, requested_video: Video, incoming: dict[str, Any]
) -> tuple[Video, dict[str, Any]]:
    """Map a save to the canonical owner and retain migration metadata."""

    workspace_video, canonical_row = get_workspace_draft(db, requested_video)
    payload = deepcopy(incoming)
    existing = (
        canonical_row.result_data
        if canonical_row is not None and isinstance(canonical_row.result_data, dict)
        else {}
    )
    metadata = _persistence_metadata(existing)
    metadata["schemaVersion"] = WORKSPACE_SCHEMA_VERSION
    metadata.setdefault("legacyDraftVideoIds", [])
    payload[WORKSPACE_PERSISTENCE_KEY] = metadata
    return workspace_video, payload
