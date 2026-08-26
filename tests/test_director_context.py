"""The clock the director reads in, and the words it anchors to.

Two things are pinned here.

**The coordinate map.** The model reasons about the cut; the timeline stores
source seconds. Every shot has to cross that boundary and a shot converted with
the wrong map lands on the wrong sentence — the failure is silent, because a
B-roll clip in the wrong place looks exactly like a B-roll clip.

**Word derivation parity.** These ids and timings mirror `segmentsToWords` in
`_lib/rough-cut-utils.ts` exactly. If they drift, an anchor resolved on the
server points at different words than the editor highlights, and the plan panel
and the timeline start disagreeing about what a shot is attached to.
"""

from __future__ import annotations

import re
import unittest

from app.services.director_context import (
    CutMap,
    build_context,
    find_quote,
    resolve_anchor,
    segments_to_words,
)


def _segment(index: int, start: float, end: float, text: str, **extra):
    segment = {"start": start, "end": end, "text": text}
    segment.update(extra)
    return segment


SEGMENTS = [
    _segment(0, 0.0, 4.0, "Most teams lose two days a week"),
    _segment(1, 4.0, 8.0, "and nobody can say where it went"),
    _segment(2, 8.0, 12.0, "so we built something to find out"),
]


class CutMapTests(unittest.TestCase):
    def test_nothing_cut_means_the_whole_take(self) -> None:
        """Empty keepRanges is what the editor means by an untouched take."""
        cut = CutMap.from_keep_ranges([], source_duration=60.0)
        self.assertEqual(cut.ranges, ((0.0, 60.0),))
        self.assertEqual(cut.runtime, 60.0)
        self.assertEqual(cut.to_director(30.0), 30.0)

    def test_runtime_is_the_kept_total_not_the_source_length(self) -> None:
        cut = CutMap.from_keep_ranges(
            [{"start": 0, "end": 10}, {"start": 20, "end": 25}], source_duration=60.0
        )
        self.assertEqual(cut.runtime, 15.0)

    def test_a_moment_after_a_cut_shifts_earlier(self) -> None:
        cut = CutMap.from_keep_ranges(
            [{"start": 0, "end": 10}, {"start": 20, "end": 30}], source_duration=60.0
        )
        # 22s of source is 2s into the second kept range, which starts at 10s
        # of director time.
        self.assertAlmostEqual(cut.to_director(22.0), 12.0)

    def test_a_moment_inside_a_cut_has_no_director_time(self) -> None:
        """It was removed. There is no honest answer but None."""
        cut = CutMap.from_keep_ranges(
            [{"start": 0, "end": 10}, {"start": 20, "end": 30}], source_duration=60.0
        )
        self.assertIsNone(cut.to_director(15.0))

    def test_director_time_maps_back_to_source(self) -> None:
        cut = CutMap.from_keep_ranges(
            [{"start": 0, "end": 10}, {"start": 20, "end": 30}], source_duration=60.0
        )
        self.assertAlmostEqual(cut.to_source(12.0), 22.0)

    def test_the_round_trip_is_exact_across_every_kept_range(self) -> None:
        """The property the whole feature rests on."""
        cut = CutMap.from_keep_ranges(
            [{"start": 3, "end": 11}, {"start": 19, "end": 24}, {"start": 40, "end": 55}],
            source_duration=90.0,
        )
        step = cut.runtime / 200.0
        for i in range(201):
            director = i * step
            with self.subTest(director=director):
                self.assertAlmostEqual(cut.to_director(cut.to_source(director)), director, places=9)

    def test_overlapping_ranges_are_merged_not_double_counted(self) -> None:
        """Double-counting would desynchronise every conversion after it."""
        cut = CutMap.from_keep_ranges(
            [{"start": 0, "end": 10}, {"start": 5, "end": 15}], source_duration=60.0
        )
        self.assertEqual(cut.ranges, ((0.0, 15.0),))
        self.assertEqual(cut.runtime, 15.0)

    def test_unsorted_ranges_are_ordered(self) -> None:
        cut = CutMap.from_keep_ranges(
            [{"start": 20, "end": 30}, {"start": 0, "end": 10}], source_duration=60.0
        )
        self.assertEqual(cut.ranges, ((0.0, 10.0), (20.0, 30.0)))

    def test_ranges_are_clamped_to_the_source(self) -> None:
        cut = CutMap.from_keep_ranges([{"start": 0, "end": 999}], source_duration=60.0)
        self.assertEqual(cut.ranges, ((0.0, 60.0),))

    def test_junk_ranges_are_ignored(self) -> None:
        cut = CutMap.from_keep_ranges(
            [{"start": "x", "end": 5}, None, {"start": 3, "end": 3}, {"start": 0, "end": 8}],
            source_duration=60.0,
        )
        self.assertEqual(cut.ranges, ((0.0, 8.0),))

    def test_a_time_past_the_end_clamps_rather_than_losing_the_shot(self) -> None:
        """A rounding error at the tail must not discard a directive."""
        cut = CutMap.from_keep_ranges([{"start": 0, "end": 10}], source_duration=60.0)
        self.assertAlmostEqual(cut.to_source(10.0001), 10.0)


class WordDerivationParityTests(unittest.TestCase):
    def test_ids_match_the_editors_scheme(self) -> None:
        """`{segmentIndex}-{wordIndex}-{segmentStart:.2f}`, as in the editor."""
        words = segments_to_words(SEGMENTS)
        self.assertEqual(words[0].id, "0-0-0.00")
        self.assertEqual(words[1].id, "0-1-0.00")

    def test_an_empty_segment_still_consumes_its_index(self) -> None:
        """The editor's forEach index is over the original array.

        Renumbering after a skipped segment would shift every id after it and
        silently break cross-referencing with the editor.
        """
        words = segments_to_words(
            [_segment(0, 0, 2, "   "), _segment(1, 2, 4, "hello there")]
        )
        self.assertEqual(words[0].id, "1-0-2.00")

    def test_real_asr_timings_are_used_when_they_line_up(self) -> None:
        segment = _segment(
            0, 0.0, 4.0, "hello there",
            words=[{"word": "hello", "start": 0.5, "end": 1.0},
                   {"word": "there", "start": 1.2, "end": 1.9}],
        )
        words = segments_to_words([segment])
        self.assertAlmostEqual(words[0].start, 0.5)
        self.assertAlmostEqual(words[1].end, 1.9)

    def test_a_mismatched_word_count_falls_back_for_the_whole_segment(self) -> None:
        """Interleaving real and synthesised timings drifts undebuggably."""
        segment = _segment(
            0, 0.0, 4.0, "hello there friend",
            words=[{"word": "hello", "start": 0.5, "end": 1.0}],
        )
        words = segments_to_words([segment])
        self.assertAlmostEqual(words[0].start, 0.0)

    def test_out_of_order_asr_timings_are_rejected(self) -> None:
        segment = _segment(
            0, 0.0, 4.0, "hello there",
            words=[{"word": "hello", "start": 3.0, "end": 3.5},
                   {"word": "there", "start": 0.1, "end": 0.5}],
        )
        words = segments_to_words([segment])
        self.assertAlmostEqual(words[0].start, 0.0)

    def test_null_asr_timings_are_rejected_like_the_editor_does(self) -> None:
        segment = _segment(
            0, 0.0, 4.0, "hello there",
            words=[{"word": "hello", "start": None, "end": 1.0},
                   {"word": "there", "start": 1.2, "end": 1.9}],
        )
        words = segments_to_words([segment])
        self.assertAlmostEqual(words[0].start, 0.0)


class TranscriptTests(unittest.TestCase):
    def test_the_transcript_is_in_director_time(self) -> None:
        """The model must only ever see the video that actually exists."""
        context = build_context(
            segments=SEGMENTS,
            keep_ranges=[{"start": 0, "end": 4}, {"start": 8, "end": 12}],
            source_duration=12.0,
            aspect="16:9",
        )
        # The middle segment was cut; the third now starts at 4s, not 8s.
        self.assertNotIn("nobody can say", context.transcript)
        self.assertIn("[s2] 4.0", context.transcript)

    def test_cut_words_are_not_offered_to_the_model(self) -> None:
        context = build_context(
            segments=SEGMENTS,
            keep_ranges=[{"start": 0, "end": 4}],
            source_duration=12.0,
            aspect="16:9",
        )
        self.assertEqual(context.segment_ids, {"s0"})

    def test_runtime_is_the_cut_length(self) -> None:
        context = build_context(
            segments=SEGMENTS,
            keep_ranges=[{"start": 0, "end": 4}, {"start": 8, "end": 12}],
            source_duration=12.0,
            aspect="16:9",
        )
        self.assertAlmostEqual(context.runtime_seconds, 8.0)

    def test_a_segment_whose_words_straddle_a_cut_still_appears(self) -> None:
        """Found on the first real run, against real ASR output.

        A surviving word can *start* inside a removed gap and only reach the
        kept range partway through. `director_words` handles that by clipping;
        `build_context` used to recompute the line's bounds from the word's raw
        source times instead, get None back, and drop the whole segment. The
        model was then shown a transcript missing lines the video still says.
        """
        context = build_context(
            segments=SEGMENTS,
            # Starts mid-way through segment 1's words and ends mid-way through
            # segment 2's, so both ends of the kept range fall inside a word.
            keep_ranges=[{"start": 5.0, "end": 9.5}],
            source_duration=12.0,
            aspect="16:9",
        )
        self.assertIn("s1", context.segment_ids)
        self.assertIn("s2", context.segment_ids)

    def test_a_line_is_never_reported_as_zero_length(self) -> None:
        """The same bug's other face: the *end* landing inside a cut collapsed
        the line to `start`, so the model saw `[s1] 0.12-0.12`."""
        context = build_context(
            segments=SEGMENTS,
            keep_ranges=[{"start": 5.0, "end": 9.5}],
            source_duration=12.0,
            aspect="16:9",
        )
        pattern = re.compile(r"^\[s\d+\]\s+([\d.]+)\u2013([\d.]+)")
        self.assertTrue(context.transcript.strip())
        for line in context.transcript.splitlines():
            found = pattern.match(line)
            with self.subTest(line=line):
                self.assertIsNotNone(found, "every line carries a readable span")
                self.assertGreater(float(found.group(2)), float(found.group(1)))

    def test_a_silent_video_reports_no_speech(self) -> None:
        """The director must not run on a video with nothing to read."""
        context = build_context(
            segments=[], keep_ranges=[], source_duration=30.0, aspect="16:9"
        )
        self.assertFalse(context.has_speech)


class AnchorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = build_context(
            segments=SEGMENTS, keep_ranges=[], source_duration=12.0, aspect="16:9"
        )

    def test_a_quote_resolves_to_the_words_that_said_it(self) -> None:
        resolved = resolve_anchor(
            self.context, segment_id="s0", quote="two days a week",
            fallback_director_start=0.0, fallback_director_end=1.0,
        )
        self.assertTrue(resolved.exact)
        words = self.context.words_by_segment[0]
        self.assertAlmostEqual(resolved.start, words[3].start)
        self.assertAlmostEqual(resolved.end, words[6].end)

    def test_punctuation_and_case_do_not_break_a_match(self) -> None:
        """The model copies verbatim, mostly; neither changes what was said."""
        resolved = resolve_anchor(
            self.context, segment_id="s0", quote="Two Days, a week!",
            fallback_director_start=0.0, fallback_director_end=1.0,
        )
        self.assertTrue(resolved.exact)

    def test_a_wrong_segment_id_is_recovered_from(self) -> None:
        """A misattributed line is a far smaller error than a misquote."""
        resolved = resolve_anchor(
            self.context, segment_id="s99", quote="where it went",
            fallback_director_start=0.0, fallback_director_end=1.0,
        )
        self.assertTrue(resolved.exact)

    def test_an_invented_quote_falls_back_and_says_so(self) -> None:
        resolved = resolve_anchor(
            self.context, segment_id="s0", quote="never said this",
            fallback_director_start=2.0, fallback_director_end=3.0,
        )
        self.assertFalse(resolved.exact)
        self.assertAlmostEqual(resolved.start, 2.0)

    def test_an_anchor_survives_a_later_recut(self) -> None:
        """The reason anchors exist at all.

        The user trims more out of the middle, so every director-time number in
        the plan now means something else — but the quote still means what it
        said, and resolves to the same source moment.
        """
        before = resolve_anchor(
            self.context, segment_id="s2", quote="to find out",
            fallback_director_start=9.0, fallback_director_end=10.0,
        )
        recut = build_context(
            segments=SEGMENTS,
            keep_ranges=[{"start": 0, "end": 2}, {"start": 8, "end": 12}],
            source_duration=12.0,
            aspect="16:9",
        )
        after = resolve_anchor(
            recut, segment_id="s2", quote="to find out",
            fallback_director_start=9.0, fallback_director_end=10.0,
        )
        self.assertTrue(after.exact)
        self.assertAlmostEqual(before.start, after.start)

    def test_find_quote_returns_none_for_an_empty_quote(self) -> None:
        self.assertIsNone(find_quote(self.context.words_by_segment[0], "   "))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
