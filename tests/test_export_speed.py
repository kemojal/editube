"""Per-clip speed in the export: rate resolution, the v1 veto, and retiming."""

from __future__ import annotations

import pytest

from app.jobs.rough_cut_export import (
    _clip_speed_rate,
    _output_span,
    _remap_segments_to_export_timeline,
    _remap_segments_with_words,
    _remap_timeline_layers_to_export,
    _segment_audio_filter,
    _speed_rates_for_ranges,
)


class TestRateResolution:
    def test_reads_and_clamps_the_inspector_rate(self):
        assert _clip_speed_rate({"speed": {"rate": 2.0}}) == 2.0
        assert _clip_speed_rate({"speed": {"rate": 9.0}}) == 4.0
        assert _clip_speed_rate({"speed": {"rate": 0.01}}) == 0.25
        assert _clip_speed_rate({"speed": {"rate": 1.0004}}) == 1.0
        assert _clip_speed_rate({}) == 1.0
        assert _clip_speed_rate(None) == 1.0

    def test_veto_masks_processed_and_compositor_clips(self):
        ranges = [(0.0, 4.0), (4.0, 8.0), (8.0, 12.0), (12.0, 16.0)]
        video_ranges = {
            (0.0, 4.0): {"speed": {"rate": 2.0}},                      # plain → allowed
            (4.0, 8.0): {"speed": {"rate": 2.0}, "rotation": 90},      # compositor → veto
            (8.0, 12.0): {"speed": {"rate": 2.0}},                     # masked → veto
            (12.0, 16.0): {"speed": {"rate": 2.0}},                    # processed → veto
        }
        masks = [{"id": "m", "sourceRange": {"start": 8.0, "end": 12.0}}]
        processed = {(12.0, 16.0): "/tmp/clip.webm"}
        rates, warnings = _speed_rates_for_ranges(
            ranges, video_ranges=video_ranges, masks=masks, processed_ranges=processed
        )
        assert rates == [2.0, 1.0, 1.0, 1.0]
        assert len(warnings) == 3
        assert all("not applied" in w for w in warnings)

    def test_output_span_compresses_by_rate(self):
        assert _output_span(2.0, 8.0, 2.0) == pytest.approx(3.0)
        assert _output_span(2.0, 8.0, 1.0) == pytest.approx(6.0)


class TestRateAwareRemaps:
    SEGMENTS = [{"start": 0.0, "end": 8.0, "text": "hello there"}]

    def test_captions_compress_inside_a_sped_range(self):
        entries = _remap_segments_to_export_timeline(
            self.SEGMENTS, [(0.0, 4.0), (4.0, 8.0)], [2.0, 1.0]
        )
        # First range plays 0–2s (4s at 2x); second occupies 2–6s.
        assert entries[0][:2] == (pytest.approx(0.0), pytest.approx(2.0))
        assert entries[1][:2] == (pytest.approx(2.0), pytest.approx(6.0))

    def test_word_timings_compress_the_same_way(self):
        segments = [
            {"start": 0.0, "end": 4.0, "text": "a b",
             "words": [{"word": "a", "start": 1.0, "end": 2.0}]},
        ]
        [entry] = _remap_segments_with_words(segments, [(0.0, 4.0)], [2.0])
        assert entry["words"][0]["start"] == pytest.approx(0.5)
        assert entry["words"][0]["end"] == pytest.approx(1.0)

    def test_layer_chunks_land_on_the_compressed_clock(self):
        layers = [
            {"id": "l1", "start": 1.0, "end": 3.0, "sourceStart": 0.0,
             "trackOrder": 0, "kind": "video"},
            {"id": "l2", "start": 5.0, "end": 6.0, "sourceStart": 0.0,
             "trackOrder": 0, "kind": "video"},
        ]
        chunks = _remap_timeline_layers_to_export(
            layers, [(0.0, 4.0), (4.0, 8.0)], source_duration=10.0, rates=[2.0, 1.0]
        )
        by_id = {chunk["id"]: chunk for chunk in chunks}
        # Inside the 2x range: 1.0–3.0 source → 0.5–1.5 output.
        assert by_id["l1"]["outputStart"] == pytest.approx(0.5)
        assert by_id["l1"]["outputEnd"] == pytest.approx(1.5)
        # After it: cursor is 2.0, so source 5.0 → 2.0 + 1.0 = 3.0.
        assert by_id["l2"]["outputStart"] == pytest.approx(3.0)

    def test_default_rates_change_nothing(self):
        with_rates = _remap_segments_to_export_timeline(
            self.SEGMENTS, [(0.0, 8.0)], [1.0]
        )
        without = _remap_segments_to_export_timeline(self.SEGMENTS, [(0.0, 8.0)])
        assert with_rates == without


class TestRetimedSegmentAudio:
    def test_atempo_sits_between_mutes_and_scaled_fades(self):
        chain = _segment_audio_filter(
            {"volume": 0, "fadeIn": 1.0, "fadeOut": 0.0},
            [(1.0, 2.0)],
            segment_start=0.0,
            segment_end=8.0,
            rate=2.0,
        )
        parts = chain.split(",")
        mute_index = next(i for i, p in enumerate(parts) if p.startswith("volume=0:enable"))
        atempo_index = next(i for i, p in enumerate(parts) if p.startswith("atempo"))
        fade_index = next(i for i, p in enumerate(parts) if p.startswith("afade=t=in"))
        assert mute_index < atempo_index < fade_index
        # The 1.0s user fade compresses to 0.5s of output.
        assert "d=0.5" in parts[fade_index]

    def test_extreme_rates_chain_atempo(self):
        chain = _segment_audio_filter(
            None, [], segment_start=0.0, segment_end=8.0, rate=4.0
        )
        assert chain.count("atempo") == 2  # 4.0 = 2.0 * 2.0

    def test_rate_one_leaves_the_chain_untouched(self):
        chain = _segment_audio_filter(
            {"fadeIn": 1.0}, [], segment_start=0.0, segment_end=8.0, rate=1.0
        )
        assert "atempo" not in chain
