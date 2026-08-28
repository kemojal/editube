from app.services.editor_feature_analytics import changed_active_editor_features


def test_editor_feature_changes_report_only_active_changed_features():
    previous = {
        "showFillers": False,
        "removeSilence": True,
        "clipAttributes": {},
        "textOverlay": {"enabled": False},
    }
    current = {
        "showFillers": True,
        "removeSilence": True,
        "clipAttributes": {
            "video:1": {
                "masks": [{"id": "mask-1", "shape": "rectangle"}],
                "removeBg": {"chromaKey": True},
            }
        },
        "textOverlay": {"enabled": True, "title": "private authored title"},
    }

    assert changed_active_editor_features(previous, current) == (
        "chroma_key",
        "filler_removal",
        "masking",
        "text_overlay",
    )


def test_editor_feature_changes_do_not_count_removal_or_unchanged_state():
    previous = {
        "removeSilence": True,
        "gridClips": [{"id": "grid-1"}],
    }
    current = {
        "removeSilence": False,
        "gridClips": [{"id": "grid-1"}],
    }

    assert changed_active_editor_features(previous, current) == ()
