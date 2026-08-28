"""Regression pins for the Phase-0 export-parity bug fixes (plan §5.3)."""

from __future__ import annotations

from app.jobs.rough_cut_export import _match_audio_range, _normalize_ranges


class TestOrderPreservation:
    def test_reordered_keep_ranges_stay_in_play_order(self):
        """Sorting by source start silently destroyed every timeline reorder."""
        ranges = [{"start": 8.0, "end": 10.0}, {"start": 2.0, "end": 5.0}]
        assert _normalize_ranges(ranges) == [(8.0, 10.0), (2.0, 5.0)]

    def test_degenerate_ranges_still_dropped_and_clamped(self):
        ranges = [
            {"start": 1.0, "end": 1.02},
            {"start": 3.0, "end": 9.0},
        ]
        assert _normalize_ranges(ranges, max_end=5.0) == [(3.0, 5.0)]


class TestTolerantRangeMatch:
    """The drift matcher now serves color/video/processed lookups too."""

    def test_exact_key_wins(self):
        table = {(0.0, 5.0): {"v": "exact"}, (0.0, 5.2): {"v": "close"}}
        assert _match_audio_range(table, 0.0, 5.0) == {"v": "exact"}

    def test_clamped_last_clip_still_finds_its_settings(self):
        # Browser said 0–5.037; ffprobe clamped the keep range to 5.0.
        table = {(0.0, 5.037): {"v": "grade"}}
        assert _match_audio_range(table, 0.0, 5.0) == {"v": "grade"}

    def test_a_span_sharing_no_edge_is_not_guessed_at(self):
        table = {(2.0, 8.0): {"v": "other-clip"}}
        assert _match_audio_range(table, 4.0, 6.0) is None


class TestMasksRequiredContract:
    def test_export_body_carries_the_fail_closed_flag(self):
        from app.api.routes.ai import RoughCutExportBody

        body = RoughCutExportBody(format="mp4", masksRequired=True)
        assert body.masksRequired is True
        assert RoughCutExportBody(format="mp4").masksRequired is False
