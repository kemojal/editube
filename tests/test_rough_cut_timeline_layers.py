from pathlib import Path

from app.jobs.rough_cut_export import (
    _extend_base_timeline_command,
    _merge_subtitle_entries,
    _normalize_timeline_layers,
    _remap_timeline_layers_to_export,
    _timeline_layers_command,
)


def _layer(**overrides):
    value = {
        "id": "copy-1",
        "clipKey": "media:copy-1",
        "kind": "video",
        "videoId": 6,
        "start": 2,
        "end": 8,
        "sourceStart": 12,
        "trackOrder": 0,
        "aboveText": True,
        "settings": {"video": {"scale": 85, "x": 4}},
    }
    value.update(overrides)
    return value


def _resolved(layer, **overrides):
    """A normalized layer as `_approved_timeline_layers` would hand it on."""
    layer.update(source="/owned/source.mp4", processed=False, hasAudio=True)
    layer.update(overrides)
    return layer


def test_timeline_layers_keep_foreign_ids_for_later_authorization():
    # Authorization moved to `_approved_timeline_layers`, which resolves ids
    # against the project. Shape sanitation must no longer silently drop a
    # clip taken from another video in the same bin.
    layers = _normalize_timeline_layers(
        [
            _layer(),
            _layer(id="foreign", clipKey="media:foreign", videoId=99),
            _layer(id="no-id", clipKey="media:no-id", videoId=None),
        ],
        video_id=6,
    )

    assert [layer["id"] for layer in layers] == ["copy-1", "foreign", "no-id"]
    assert [layer["videoId"] for layer in layers] == [6, 99, 6]
    assert all("sourceUrl" not in layer for layer in layers)


def test_timeline_layers_allow_positions_past_the_primary_media():
    # Appended clips sit past the A-roll on purpose. Bounding them by the
    # primary's duration used to collapse them to nothing.
    layers = _normalize_timeline_layers(
        [_layer(id="tail", clipKey="media:tail", start=30, end=42, sourceStart=0)],
        video_id=6,
    )

    assert layers[0]["start"] == 30
    assert layers[0]["end"] == 42


def test_timeline_layers_reject_malformed_and_unbounded_timing():
    layers = _normalize_timeline_layers(
        [
            _layer(id="nan", clipKey="media:nan", start="oops"),
            _layer(id="inverted", clipKey="media:inverted", start=9, end=9),
            _layer(id="huge", clipKey="media:huge", start=0, end=1e9),
            _layer(id="unkeyed", clipKey="clip:unkeyed"),
        ],
        video_id=6,
    )

    assert [layer["id"] for layer in layers] == ["huge"]
    assert layers[0]["end"] == 24 * 60 * 60.0


def test_timeline_layer_audio_flag_is_opt_in():
    layers = _normalize_timeline_layers(
        [
            _layer(),
            _layer(id="loud", clipKey="media:loud", audioEnabled=True),
        ],
        video_id=6,
    )

    assert [layer["audioEnabled"] for layer in layers] == [False, True]


def test_timeline_layer_chunks_follow_keep_range_ripple_and_track_order():
    layers = _normalize_timeline_layers(
        [
            _layer(id="top", clipKey="media:top", trackOrder=0),
            _layer(id="bottom", clipKey="media:bottom", trackOrder=3),
        ],
        video_id=6,
    )
    for layer in layers:
        _resolved(layer)

    chunks = _remap_timeline_layers_to_export(layers, [(0, 4), (6, 10)])

    assert [chunk["id"] for chunk in chunks] == ["bottom", "bottom", "top", "top"]
    top = [chunk for chunk in chunks if chunk["id"] == "top"]
    assert [(chunk["outputStart"], chunk["outputEnd"]) for chunk in top] == [(2, 4), (4, 6)]
    assert [chunk["sourceSeek"] for chunk in top] == [12, 16]


def test_tail_layers_map_one_for_one_after_the_kept_total():
    # 30s source with 0-4 and 6-10 kept: 8s of programme, then the tail.
    layers = _normalize_timeline_layers(
        [_layer(id="tail", clipKey="media:tail", start=30, end=36, sourceStart=5)],
        video_id=6,
    )
    _resolved(layers[0])

    chunks = _remap_timeline_layers_to_export(
        layers, [(0, 4), (6, 10)], source_duration=30
    )

    assert len(chunks) == 1
    assert (chunks[0]["outputStart"], chunks[0]["outputEnd"]) == (8, 14)
    assert chunks[0]["sourceSeek"] == 5


def test_a_clip_straddling_the_source_end_renders_on_both_sides():
    layers = _normalize_timeline_layers(
        [_layer(id="over", clipKey="media:over", start=8, end=34, sourceStart=0)],
        video_id=6,
    )
    _resolved(layers[0])

    chunks = _remap_timeline_layers_to_export(
        layers, [(0, 10)], source_duration=10
    )

    # The part inside the source ripples with the cuts; the rest is tail.
    assert [(c["outputStart"], c["outputEnd"]) for c in chunks] == [(8, 10), (10, 34)]
    assert [c["clipOffset"] for c in chunks] == [0, 2]


def test_tail_is_ignored_without_a_known_source_duration():
    layers = _normalize_timeline_layers(
        [_layer(id="tail", clipKey="media:tail", start=30, end=36)],
        video_id=6,
    )
    _resolved(layers[0])

    assert _remap_timeline_layers_to_export(layers, [(0, 10)]) == []


def test_timeline_compositor_maps_base_audio_and_renders_top_chunk_last():
    layers = _normalize_timeline_layers([_layer()], video_id=6)
    _resolved(layers[0])
    chunks = _remap_timeline_layers_to_export(layers, [(0, 10)])
    command = _timeline_layers_command(
        base_video=Path("/tmp/base.mp4"),
        chunks=chunks,
        scale_w=1920,
        scale_h=1080,
        frame_rate=30,
        crf=23,
        output=Path("/tmp/layered.mp4"),
    )

    graph = command[command.index("-filter_complex") + 1]
    assert "[timeline_base][layer0_timed]overlay=" in graph
    assert "[timeline_composite_0]format=yuv420p[v]" in graph
    # Nothing audible to add, so the base stream is still copied untouched.
    assert command[command.index("-map", command.index("[v]")) + 1] == "0:a?"
    assert command[command.index("-c:a") + 1] == "copy"
    assert "amix" not in graph
    assert "https://" not in " ".join(command)


def test_audible_layers_are_mixed_over_the_base_at_their_own_offsets():
    layers = _normalize_timeline_layers(
        [_layer(id="quote", clipKey="media:quote", start=4, end=9, audioEnabled=True)],
        video_id=6,
    )
    _resolved(layers[0])
    chunks = _remap_timeline_layers_to_export(layers, [(0, 20)])
    command = _timeline_layers_command(
        base_video=Path("/tmp/base.mp4"),
        chunks=chunks,
        scale_w=1920,
        scale_h=1080,
        frame_rate=30,
        crf=23,
        output=Path("/tmp/layered.mp4"),
    )

    graph = command[command.index("-filter_complex") + 1]
    assert "[0:a]" in graph
    assert "adelay=delays=4000:all=1[amix_layer0]" in graph
    assert "amix=inputs=2:normalize=0:dropout_transition=0[a]" in graph
    assert command[command.index("-map", command.index("[v]")) + 1] == "[a]"
    assert command[command.index("-c:a") + 1] == "aac"


def test_a_silent_source_layer_is_never_referenced_in_the_audio_graph():
    # `[N:a]` on a stream that does not exist fails the whole command, so a
    # clip whose media has no audio must not reach the mixer at all.
    layers = _normalize_timeline_layers(
        [_layer(id="mute", clipKey="media:mute", audioEnabled=True)],
        video_id=6,
    )
    _resolved(layers[0], hasAudio=False)
    chunks = _remap_timeline_layers_to_export(layers, [(0, 20)])
    command = _timeline_layers_command(
        base_video=Path("/tmp/base.mp4"),
        chunks=chunks,
        scale_w=1920,
        scale_h=1080,
        frame_rate=30,
        crf=23,
        output=Path("/tmp/layered.mp4"),
    )

    graph = command[command.index("-filter_complex") + 1]
    assert "amix" not in graph
    assert "[1:a]" not in graph


def test_layer_audio_alone_carries_the_mix_when_the_base_is_silent():
    layers = _normalize_timeline_layers(
        [_layer(id="quote", clipKey="media:quote", audioEnabled=True)],
        video_id=6,
    )
    _resolved(layers[0])
    chunks = _remap_timeline_layers_to_export(layers, [(0, 20)])
    command = _timeline_layers_command(
        base_video=Path("/tmp/base.mp4"),
        chunks=chunks,
        scale_w=1920,
        scale_h=1080,
        frame_rate=30,
        crf=23,
        output=Path("/tmp/layered.mp4"),
        base_has_audio=False,
    )

    graph = command[command.index("-filter_complex") + 1]
    assert "[0:a]" not in graph
    # A one-input mix is a rename, not an amix.
    assert "amix=" not in graph
    assert "[amix_layer0]anull[a]" in graph


def test_extending_the_base_pads_picture_and_sound_together():
    command = _extend_base_timeline_command(
        base_video=Path("/tmp/base.mp4"),
        tail_duration=6.5,
        has_audio=True,
        frame_rate=30,
        crf=23,
        output=Path("/tmp/tail.mp4"),
    )

    graph = command[command.index("-filter_complex") + 1]
    assert "tpad=stop_mode=add:stop_duration=6.500000:color=black[v]" in graph
    assert "apad=pad_dur=6.500000[a]" in graph
    assert command[command.index("-map", command.index("[v]")) + 1] == "[a]"


def test_merging_captions_leaves_the_a_roll_alone_when_nothing_was_added():
    base = [(0.0, 3.0, "one"), (4.0, 6.0, "two")]
    assert _merge_subtitle_entries(base, []) == base


def test_a_clip_caption_swallows_the_a_roll_cue_it_covers():
    merged = _merge_subtitle_entries(
        [(2.0, 5.0, "a-roll")],
        [(1.0, 6.0, "clip")],
    )
    assert merged == [(1.0, 6.0, "clip")]


def test_a_clip_caption_splits_the_a_roll_cue_around_it():
    merged = _merge_subtitle_entries(
        [(0.0, 10.0, "a-roll")],
        [(4.0, 6.0, "clip")],
    )
    assert merged == [(0.0, 4.0, "a-roll"), (4.0, 6.0, "clip"), (6.0, 10.0, "a-roll")]


def test_slivers_left_by_an_overlap_are_dropped_rather_than_flashed():
    # A fifth of a second of text on screen reads as a glitch, not a caption.
    merged = _merge_subtitle_entries(
        [(0.0, 5.1, "a-roll")],
        [(0.1, 5.0, "clip")],
    )
    assert merged == [(0.1, 5.0, "clip")]


def test_a_caption_that_does_not_overlap_is_untouched():
    merged = _merge_subtitle_entries(
        [(0.0, 4.0, "a-roll")],
        [(8.0, 9.0, "clip")],
    )
    assert merged == [(0.0, 4.0, "a-roll"), (8.0, 9.0, "clip")]


def test_extending_a_silent_base_stays_silent():
    command = _extend_base_timeline_command(
        base_video=Path("/tmp/base.mp4"),
        tail_duration=2,
        has_audio=False,
        frame_rate=30,
        crf=23,
        output=Path("/tmp/tail.mp4"),
    )

    graph = command[command.index("-filter_complex") + 1]
    assert "apad" not in graph
    assert "[a]" not in command
    assert "-an" in command
