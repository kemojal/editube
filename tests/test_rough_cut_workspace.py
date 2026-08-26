from copy import deepcopy

from app.db.models import AiResult
from app.services.rough_cut_workspace import (
    WORKSPACE_PERSISTENCE_KEY,
    WORKSPACE_SCHEMA_VERSION,
    get_workspace_draft,
    prepare_workspace_save,
)


def _draft(video_id: int, payload: dict) -> AiResult:
    return AiResult(
        video_id=video_id,
        result_type="rough_cut_draft",
        status="completed",
        result_data=payload,
    )


def test_workspace_draft_merges_asset_layers_into_canonical_source_once(
    db_session, make_project, make_video
):
    project = make_project()
    source = make_video(project=project, description="Rough cut source")
    asset = make_video(project=project, description="Rough cut asset")
    db_session.add_all(
        [
            _draft(
                source.id,
                {
                    "keepRanges": [{"id": "source-cut", "start": 0, "end": 8}],
                    "timelineMediaItems": [
                        {"id": "source-overlay", "trackId": "V2"}
                    ],
                    "timelineTracks": [{"id": "V1", "kind": "video"}],
                    "clipAttributes": {"source-cut": {"opacity": 0.8}},
                },
            ),
            _draft(
                asset.id,
                {
                    # Source-owned cuts must never be replaced by an asset draft.
                    "keepRanges": [{"id": "stale-cut", "start": 0, "end": 2}],
                    "timelineMediaItems": [
                        {"id": "asset-overlay", "trackId": "V3"}
                    ],
                    "timelineTracks": [{"id": "V3", "kind": "video"}],
                    "lowerThirds": [{"id": "legacy-title", "start": 1, "end": 4}],
                    "clipAttributes": {"asset-overlay": {"opacity": 0.5}},
                },
            ),
        ]
    )
    db_session.commit()

    workspace_video, row = get_workspace_draft(db_session, asset)

    assert workspace_video.id == source.id
    assert row is not None
    assert row.video_id == source.id
    payload = row.result_data
    assert payload["keepRanges"] == [
        {"id": "source-cut", "start": 0, "end": 8}
    ]
    assert {item["id"] for item in payload["timelineMediaItems"]} == {
        "source-overlay",
        "asset-overlay",
    }
    assert {item["id"] for item in payload["timelineTracks"]} == {"V1", "V3"}
    assert payload["lowerThirds"] == [
        {"id": "legacy-title", "start": 1, "end": 4}
    ]
    assert set(payload["clipAttributes"]) == {"source-cut", "asset-overlay"}
    assert payload[WORKSPACE_PERSISTENCE_KEY] == {
        "schemaVersion": WORKSPACE_SCHEMA_VERSION,
        "legacyDraftVideoIds": [asset.id],
    }

    # A subsequent save may intentionally remove a recovered overlay. The
    # migration marker ensures the stale asset draft cannot resurrect it.
    saved_payload = deepcopy(payload)
    saved_payload["timelineMediaItems"] = []
    row.result_data = saved_payload
    db_session.commit()

    _, reopened = get_workspace_draft(db_session, asset)

    assert reopened is not None
    assert reopened.result_data["timelineMediaItems"] == []


def test_prepare_workspace_save_maps_asset_url_to_source_and_keeps_metadata(
    db_session, make_project, make_video
):
    project = make_project()
    source = make_video(project=project, description="Rough cut source")
    asset = make_video(project=project, description="  ROUGH CUT ASSET ")
    db_session.add(
        _draft(
            source.id,
            {
                "timelineMediaItems": [],
                WORKSPACE_PERSISTENCE_KEY: {
                    "schemaVersion": WORKSPACE_SCHEMA_VERSION,
                    "legacyDraftVideoIds": [asset.id],
                },
            },
        )
    )
    db_session.commit()

    workspace_video, payload = prepare_workspace_save(
        db_session,
        asset,
        {"timelineMediaItems": [{"id": "new-overlay", "trackId": "V2"}]},
    )

    assert workspace_video.id == source.id
    assert payload["timelineMediaItems"] == [
        {"id": "new-overlay", "trackId": "V2"}
    ]
    assert payload[WORKSPACE_PERSISTENCE_KEY] == {
        "schemaVersion": WORKSPACE_SCHEMA_VERSION,
        "legacyDraftVideoIds": [asset.id],
    }

