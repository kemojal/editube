"""The export pipeline's audio: layer fades, clamped ranges, audio lanes.

Everything under test here is pure -- chunk dicts in, ffmpeg filter strings
out -- so the assertions are on the generated graph, the same way the burn-in
tests hold `_burn_in_overlay_command` to its contract. Nothing runs ffmpeg and
nothing touches the database.
"""

from pathlib import Path

import pytest

from app.jobs.rough_cut_export import (
    _audio_range_settings,
    _layer_audio_filter_parts,
    _match_audio_range,
    _normalize_timeline_layers,
    _remap_timeline_layers_to_export,
    _timeline_audio_graph,
    _timeline_audio_mix_command,
    _timeline_layers_command,
)


def _layer(**overrides):
    value = {
        "id": "bed-1",
        "clipKey": "media:bed-1",
        "kind": "video",
        "videoId": 6,
        "start": 0,
        "end": 10,
        "sourceStart": 0,
        "trackOrder": 0,
        "aboveText": True,
        "audioEnabled": True,
        "settings": {"audio": {"fadeIn": 2, "fadeOut": 2}},
    }
    value.update(overrides)
    return value


def _resolved(layer, **overrides):
    """A normalized layer as `_approved_timeline_layers` would hand it on."""
    layer.update(source="/owned/source.mp4", processed=False, hasAudio=True, isStill=False)
    layer.update(overrides)
    return layer


def _chunks(layers, kept_ranges, **kwargs):
    normalized = _normalize_timeline_layers(layers, video_id=6)
    for layer in normalized:
        _resolved(layer)
    return _remap_timeline_layers_to_export(normalized, kept_ranges, **kwargs)


def _fade_chain(chunk):
    """The one chunk's own branch of the mix graph, before it reaches amix."""
    graph, _ = _timeline_audio_graph([chunk], base_has_audio=True)
    return next(line for line in graph if line.endswith("[amix_layer0]"))


# -- Defect 1: a clip's fades belong to the clip, not to each fragment -------


def test_a_layer_split_by_a_cut_still_carries_its_whole_span():
    chunks = _chunks([_layer()], [(0, 4), (5, 10)])

    assert [(c["outputStart"], c["outputEnd"]) for c in chunks] == [(0, 4), (4, 9)]
    # Both fragments describe the same 10s clip, at different offsets into it.
    assert [c["layerDuration"] for c in chunks] == [10, 10]
    assert [c["layerOffset"] for c in chunks] == [0, 5]


def test_a_cut_layer_fades_once_across_the_clip_not_once_per_chunk():
    # B-roll 0->10 with 2s fades; the user cuts the A-roll 4->5. Feeding each
    # chunk its own length made both fragments fade in AND out, so the bed
    # dipped to silence in the middle of the clip.
    chunks = _chunks([_layer()], [(0, 4), (5, 10)])
    first, second = (_fade_chain(chunk) for chunk in chunks)

    # The opening fragment holds the fade-in and is nowhere near the fade-out.
    assert "afade=t=in:st=0:d=2.000000" in first
    assert "afade=t=out" not in first
    # The closing fragment is past the fade-in entirely and fades out at
    # layer-local 8s, which is 3s into its own 5s span.
    assert "afade=t=in" not in second
    assert "afade=t=out:st=3.000000:d=2.000000" in second


def test_an_uncut_layer_is_unchanged_by_the_layer_span_fix():
    chunks = _chunks([_layer()], [(0, 20)])
    chain = _fade_chain(chunks[0])

    assert "afade=t=in:st=0:d=2.000000" in chain
    assert "afade=t=out:st=8.000000:d=2.000000" in chain


def test_a_chunk_opening_midway_up_the_ramp_resumes_the_ramp():
    # The cut lands INSIDE the fade-in. Clamping `afade`'s `st` back to zero
    # would restart the fade from silence -- the very dip being fixed -- so
    # the remaining part of the ramp is expressed as a gain curve instead.
    parts = _layer_audio_filter_parts(
        {"fadeIn": 4},
        layer_duration=10,
        chunk_offset=1,
        chunk_duration=9,
    )

    assert parts == ["volume='min(1,(t+1.000000)/4.000000)':eval=frame"]


def test_a_chunk_opening_midway_down_the_ramp_continues_falling():
    parts = _layer_audio_filter_parts(
        {"fadeOut": 4},
        layer_duration=10,
        chunk_offset=8,
        chunk_duration=2,
    )

    assert parts == [
        "volume='max(0,min(1,(10.000000-(t+8.000000))/4.000000))':eval=frame"
    ]


def test_a_fade_that_falls_outside_a_chunk_is_not_emitted_at_all():
    # Layer-local [4, 6] of a 10s clip: past a 2s fade-in, before the fade-out.
    assert (
        _layer_audio_filter_parts(
            {"fadeIn": 2, "fadeOut": 2},
            layer_duration=10,
            chunk_offset=4,
            chunk_duration=2,
        )
        == []
    )


def test_gain_still_applies_to_every_fragment_of_a_cut_layer():
    # Volume is a constant over the clip, so unlike a fade it belongs on each
    # fragment -- and it stays last in the chain.
    chunks = _chunks(
        [_layer(settings={"audio": {"fadeIn": 2, "fadeOut": 2, "volume": -6}})],
        [(0, 4), (5, 10)],
    )
    for chunk in chunks:
        chain = _fade_chain(chunk)
        # Gain sits after the fades and immediately before the delay.
        assert (
            "volume=0.501187,adelay=delays=%d:all=1[amix_layer0]"
            % round(chunk["outputStart"] * 1000)
        ) in chain


def test_a_layer_that_lands_past_the_source_fades_across_both_halves():
    # 8->34 on a 10s source: 8->10 ripples with the cuts, the rest is tail.
    chunks = _chunks(
        [_layer(id="over", clipKey="media:over", start=8, end=34)],
        [(0, 10)],
        source_duration=10,
    )

    assert [c["layerDuration"] for c in chunks] == [26, 26]
    assert [c["layerOffset"] for c in chunks] == [0, 2]
    first, second = (_fade_chain(chunk) for chunk in chunks)
    assert "afade=t=in:st=0:d=2.000000" in first
    assert "afade=t=out" not in first
    # Fade-out at layer-local 24s == 22s into the tail fragment.
    assert "afade=t=out:st=22.000000:d=2.000000" in second


# -- Defect 2: a keep range clamped by the probe still finds its settings ----


def test_a_clamped_keep_range_still_matches_its_audio_settings():
    # The browser reported the media 40ms longer than ffprobe measured it, so
    # `_normalize_ranges` clamped the last keep range's end. The exact-key
    # lookup missed and the last clip exported with no volume and no fades.
    table = _audio_range_settings(
        [{"start": 0, "end": 4, "volume": -3}, {"start": 8, "end": 12.04, "fadeOut": 1.5}]
    )

    assert _match_audio_range(table, 8.0, 12.0) == {
        "volume": None,
        "fadeIn": None,
        "fadeOut": 1.5,
    }


def test_an_exact_range_still_wins_over_a_neighbour_that_touches_it():
    table = _audio_range_settings(
        [{"start": 0, "end": 4, "volume": -3}, {"start": 4, "end": 8, "volume": 6}]
    )

    assert _match_audio_range(table, 4.0, 8.0)["volume"] == 6
    assert _match_audio_range(table, 0.0, 4.0)["volume"] == -3


def test_a_span_that_lines_up_with_neither_edge_is_left_unmatched():
    # Overlapping is not the same as being the same clip; guessing here would
    # put one clip's gain on another.
    table = _audio_range_settings([{"start": 0, "end": 30, "volume": -12}])

    assert _match_audio_range(table, 10.0, 20.0) is None


def test_nothing_overlapping_matches_nothing():
    table = _audio_range_settings([{"start": 0, "end": 4, "volume": -3}])

    assert _match_audio_range(table, 20.0, 24.0) is None


def test_a_range_beyond_the_tolerance_is_not_forced_to_match():
    table = _audio_range_settings([{"start": 8, "end": 12, "volume": -3}])

    assert _match_audio_range(table, 8.5, 12.5) is None


# -- Defect 3: audio-lane clips reach the mix -------------------------------


def test_an_audio_lane_clip_is_audible_without_an_explicit_flag():
    # `audioEnabled` is opt-in for picture (B-roll ships muted) and opt-out
    # for an audio lane, whose only content is its sound.
    layers = _normalize_timeline_layers(
        [
            _layer(kind="audio", audioEnabled=None),
            _layer(id="sfx", clipKey="media:sfx", kind="audio", audioEnabled=False),
            _layer(id="broll", clipKey="media:broll", kind="video", audioEnabled=None),
        ],
        video_id=6,
    )

    assert [layer["audioEnabled"] for layer in layers] == [True, False, False]


def test_an_audio_lane_clip_is_mixed_but_never_composited():
    chunks = _chunks(
        [
            _layer(id="pic", clipKey="media:pic", kind="video", trackOrder=0),
            _layer(id="music", clipKey="media:music", kind="audio", trackOrder=1),
        ],
        [(0, 20)],
    )
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

    # Bottom track first: the music lane is input 1, the picture is input 2.
    assert [chunk["id"] for chunk in chunks] == ["music", "pic"]
    # A picture-less input must never be asked for frames.
    assert "[1:v]" not in graph
    assert "[2:v]" in graph
    # ...but both are in the mix, at their own inputs.
    assert "[1:a]" in graph and "[2:a]" in graph
    assert "amix=inputs=3:normalize=0:dropout_transition=0[a]" in graph
    assert command[command.index("-c:a") + 1] == "aac"


def test_a_pass_carrying_only_audio_lanes_copies_the_picture_through():
    chunks = _chunks([_layer(id="music", clipKey="media:music", kind="audio")], [(0, 20)])
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

    assert "overlay=" not in graph
    assert "[timeline_base]" not in graph
    assert command[command.index("-map") + 1] == "0:v"
    assert command[command.index("-c:v") + 1] == "copy"
    assert command[command.index("-map", command.index("0:v")) + 1] == "[a]"
    assert "adelay=delays=0:all=1[amix_layer0]" in graph


def test_a_silent_source_on_an_audio_lane_never_reaches_the_mixer():
    normalized = _normalize_timeline_layers(
        [_layer(id="music", clipKey="media:music", kind="audio")], video_id=6
    )
    _resolved(normalized[0], hasAudio=False)
    chunks = _remap_timeline_layers_to_export(normalized, [(0, 20)])

    assert _timeline_audio_graph(chunks, base_has_audio=True) == ([], None)


def test_the_wav_path_mixes_audio_lanes_onto_the_merged_a_roll():
    # A WAV export never runs a compositing pass, so without this the music
    # the editor was playing is simply absent from the download.
    chunks = _chunks(
        [_layer(id="music", clipKey="media:music", kind="audio", start=4, end=14)],
        [(0, 30)],
    )
    command = _timeline_audio_mix_command(
        base_audio=Path("/tmp/merged.wav"),
        chunks=chunks,
        output=Path("/tmp/mixed.wav"),
    )
    graph = command[command.index("-filter_complex") + 1]

    assert command[:4] == ["ffmpeg", "-y", "-i", "/tmp/merged.wav"]
    assert command[command.index("-i", 4) - 1] == "10.000000"  # -t, the clip span
    assert "adelay=delays=4000:all=1[amix_layer0]" in graph
    assert "afade=t=in:st=0:d=2.000000" in graph
    assert command[command.index("-map") + 1] == "[a]"
    assert command[command.index("-acodec") + 1] == "pcm_s16le"


def test_the_wav_mix_is_skipped_when_no_lane_actually_sounds():
    normalized = _normalize_timeline_layers(
        [_layer(id="music", clipKey="media:music", kind="audio")], video_id=6
    )
    _resolved(normalized[0], hasAudio=False)
    chunks = _remap_timeline_layers_to_export(normalized, [(0, 20)])

    assert _timeline_audio_mix_command(
        base_audio=Path("/tmp/merged.wav"),
        chunks=chunks,
        output=Path("/tmp/mixed.wav"),
    ) == []


@pytest.mark.parametrize("kind", ["audio", "video", "image"])
def test_no_layer_kind_can_smuggle_a_url_into_the_command(kind):
    normalized = _normalize_timeline_layers(
        [_layer(kind=kind, sourceUrl="https://evil.test/x.mp4")], video_id=6
    )
    assert all("sourceUrl" not in layer for layer in normalized)
