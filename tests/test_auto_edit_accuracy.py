"""Accuracy features: fuzzy word alignment, VAD/word-gap silences, retakes,
weak-filler detachment, and word-preserving transcript edits.

The word data in several tests mirrors a real faster-whisper output where
"cross-platform." arrived as two words ("cross" + "-platform.") and a 2s
pause hid inside one segment — the exact shapes that used to defeat the
count-equality guard.
"""

import unittest

from app.services.auto_edit import _analyze_segments
from app.services.word_alignment import (
    align_tokens_to_words,
    realign_words_to_text,
    timed_words_for_tokens,
)


def _blip_segment() -> dict:
    return {
        "start": 7.7,
        "end": 13.52,
        "text": "That's why I use Blip. It's basically AirDrop but cross-platform.",
        "words": [
            {"word": "That's", "start": 7.7, "end": 8.02},
            {"word": "why", "start": 8.02, "end": 8.22},
            {"word": "I", "start": 8.22, "end": 8.36},
            {"word": "use", "start": 8.36, "end": 8.5},
            {"word": "Blip.", "start": 8.5, "end": 8.84},
            {"word": "It's", "start": 10.86, "end": 11.14},
            {"word": "basically", "start": 11.14, "end": 11.5},
            {"word": "AirDrop", "start": 11.5, "end": 12.06},
            {"word": "but", "start": 12.06, "end": 12.78},
            {"word": "cross", "start": 12.78, "end": 13.06},
            {"word": "-platform.", "start": 13.06, "end": 13.52},
        ],
    }


class WordAlignmentTests(unittest.TestCase):
    def test_split_compound_word_is_merged_not_discarded(self):
        seg = _blip_segment()
        tokens = seg["text"].split()  # 10 tokens vs 11 ASR words
        timed = timed_words_for_tokens(tokens, seg["words"], seg_start=7.7, seg_end=13.52)

        self.assertEqual(len(timed), 10)
        # Every non-split token keeps its exact ASR timing…
        self.assertAlmostEqual(timed[4][1], 8.5, places=3)   # Blip.
        self.assertAlmostEqual(timed[5][1], 10.86, places=3)  # It's — 2s pause preserved
        # …and the split compound spans both ASR words.
        self.assertAlmostEqual(timed[9][1], 12.78, places=3)
        self.assertAlmostEqual(timed[9][2], 13.52, places=3)

    def test_matched_flag_distinguishes_real_from_interpolated(self):
        seg = _blip_segment()
        aligned = align_tokens_to_words(
            seg["text"].split(), seg["words"], seg_start=7.7, seg_end=13.52
        )
        self.assertTrue(all(a["matched"] for a in aligned))

        no_words = align_tokens_to_words(["hello", "there"], None, seg_start=0.0, seg_end=1.0)
        self.assertFalse(any(a["matched"] for a in no_words))

    def test_realign_preserves_surviving_words_after_edit(self):
        seg = _blip_segment()
        new_words = realign_words_to_text(
            "That's why I use Blip. It is basically AirDrop but cross-platform.",
            seg["words"],
            seg_start=7.7,
            seg_end=13.52,
        )
        by_text = {w["word"]: w for w in new_words}
        self.assertAlmostEqual(by_text["Blip."]["end"], 8.84, places=3)
        self.assertAlmostEqual(by_text["basically"]["start"], 11.14, places=3)
        # The rewrite ("It's" → "It is") inherits the replaced word's envelope,
        # not the whole 2s pause before it.
        self.assertGreaterEqual(by_text["It"]["start"], 10.85)
        self.assertLessEqual(by_text["is"]["end"], 11.15)

    def test_realign_returns_none_without_usable_words(self):
        self.assertIsNone(realign_words_to_text("hello", None, seg_start=0, seg_end=1))
        self.assertIsNone(
            realign_words_to_text(
                "hello", [{"word": "x", "start": "bad", "end": 1}], seg_start=0, seg_end=1
            )
        )

    def test_monotonic_output_even_with_jittered_input(self):
        words = [
            {"word": "a", "start": 0.0, "end": 0.5},
            {"word": "b", "start": 0.48, "end": 0.9},  # slight overlap is real Whisper behaviour
            {"word": "c", "start": 0.9, "end": 1.4},
        ]
        timed = timed_words_for_tokens(["a", "b", "c"], words, seg_start=0.0, seg_end=1.4)
        for prev, cur in zip(timed, timed[1:]):
            self.assertGreaterEqual(cur[1], prev[1])


class IntraSegmentSilenceTests(unittest.TestCase):
    def test_word_gap_silence_inside_single_segment_is_found(self):
        # One segment, contiguous text, but the words reveal a 2.4s hole —
        # segment-gap analysis by definition saw nothing here.
        seg = {
            "start": 0.0,
            "end": 6.0,
            "text": "hello there friends",
            "words": [
                {"word": "hello", "start": 0.0, "end": 0.5},
                {"word": "there", "start": 0.5, "end": 1.0},
                {"word": "friends", "start": 3.4, "end": 6.0},
            ],
        }
        analysis = _analyze_segments([seg], duration=6.0)
        silences = [s for s in analysis["suggestions"] if s["kind"] == "silence"]
        self.assertEqual(len(silences), 1)
        self.assertAlmostEqual(silences[0]["start"], 1.0 + 0.08, places=3)
        self.assertAlmostEqual(silences[0]["end"], 3.4 - 0.08, places=3)
        self.assertEqual(silences[0]["severity"], "high")
        # And the dead air actually leaves keepRanges.
        keeps = analysis["keepRanges"]
        self.assertEqual(len(keeps), 2)
        self.assertAlmostEqual(keeps[0]["end"], 1.08, places=3)
        self.assertAlmostEqual(keeps[1]["start"], 3.32, places=3)

    def test_segment_gap_still_detected_without_words(self):
        segments = [
            {"start": 0.0, "end": 2.0, "text": "first part"},
            {"start": 3.0, "end": 5.0, "text": "second part"},
        ]
        analysis = _analyze_segments(segments, duration=5.0)
        silences = [s for s in analysis["suggestions"] if s["kind"] == "silence"]
        self.assertEqual(len(silences), 1)
        self.assertAlmostEqual(silences[0]["start"], 2.08, places=3)
        self.assertAlmostEqual(silences[0]["end"], 2.92, places=3)


class VadSilenceTests(unittest.TestCase):
    def test_vad_silences_replace_gap_inference(self):
        segments = [
            {"start": 0.0, "end": 2.0, "text": "first part"},
            {"start": 3.0, "end": 5.0, "text": "second part"},
        ]
        # VAD found a different (real) silence than the transcript gap implies.
        analysis = _analyze_segments(
            segments, duration=5.0, vad_silences=[[1.8, 3.1], [4.0, 4.2]]
        )
        silences = [s for s in analysis["suggestions"] if s["kind"] == "silence"]
        self.assertEqual(len(silences), 1)  # the 0.2s one is under threshold
        self.assertEqual(silences[0].get("source"), "vad")
        self.assertAlmostEqual(silences[0]["start"], 1.8 + 0.04, places=3)
        self.assertAlmostEqual(silences[0]["end"], 3.1 - 0.04, places=3)

    def test_vad_silences_respect_aggressiveness_threshold(self):
        segments = [{"start": 0.0, "end": 5.0, "text": "one continuous take"}]
        vad = [[2.0, 2.5]]  # 0.5s pause
        light = _analyze_segments(segments, 5.0, aggressiveness="light", vad_silences=vad)
        aggressive = _analyze_segments(segments, 5.0, aggressiveness="aggressive", vad_silences=vad)
        self.assertEqual(
            len([s for s in light["suggestions"] if s["kind"] == "silence"]), 0
        )
        self.assertEqual(
            len([s for s in aggressive["suggestions"] if s["kind"] == "silence"]), 1
        )

    def test_dict_shaped_vad_silences_accepted(self):
        segments = [{"start": 0.0, "end": 5.0, "text": "one continuous take"}]
        analysis = _analyze_segments(
            segments, 5.0, vad_silences=[{"start": 1.0, "end": 2.0}]
        )
        silences = [s for s in analysis["suggestions"] if s["kind"] == "silence"]
        self.assertEqual(len(silences), 1)

    def test_remove_silences_false_ignores_vad(self):
        segments = [{"start": 0.0, "end": 5.0, "text": "one continuous take"}]
        analysis = _analyze_segments(
            segments, 5.0, remove_silences=False, vad_silences=[[1.0, 3.0]]
        )
        self.assertEqual(
            [s for s in analysis["suggestions"] if s["kind"] == "silence"], []
        )
        self.assertEqual(analysis["keepRanges"], [{"start": 0.0, "end": 5.0}])


class RetakeDetectionTests(unittest.TestCase):
    def test_near_duplicate_consecutive_sentence_flags_earlier(self):
        segments = [
            {"start": 0.0, "end": 3.0, "text": "Welcome to my channel where we build things."},
            {"start": 3.5, "end": 6.5, "text": "Welcome to my channel where we make things."},
        ]
        analysis = _analyze_segments(segments, 6.5, remove_silences=False)
        repeats = [s for s in analysis["suggestions"] if s["kind"] == "repeat"]
        self.assertEqual(len(repeats), 1)
        self.assertEqual(repeats[0]["start"], 0.0)
        self.assertEqual(repeats[0]["end"], 3.0)
        self.assertEqual(analysis["keepRanges"], [{"start": 3.5, "end": 6.5}])

    def test_abandoned_prefix_start_flags_earlier(self):
        segments = [
            {"start": 0.0, "end": 1.5, "text": "Welcome to my channel"},
            {"start": 2.0, "end": 5.0, "text": "Welcome to my channel, where we build useful things together."},
        ]
        analysis = _analyze_segments(segments, 5.0, remove_silences=False)
        repeats = [s for s in analysis["suggestions"] if s["kind"] == "repeat"]
        self.assertEqual(len(repeats), 1)
        self.assertEqual(repeats[0]["start"], 0.0)

    def test_dissimilar_sentences_are_not_retakes(self):
        segments = [
            {"start": 0.0, "end": 3.0, "text": "Welcome to my channel where we build things."},
            {"start": 3.5, "end": 6.5, "text": "Today we are testing silence detection accuracy."},
        ]
        analysis = _analyze_segments(segments, 6.5, remove_silences=False)
        self.assertEqual(
            [s for s in analysis["suggestions"] if s["kind"] == "repeat"], []
        )

    def test_rhetorical_chain_is_not_a_stutter(self):
        # "Windows to Mac, Mac to iPhone, iPhone to Android" repeats words on
        # purpose — clause punctuation + a spoken beat separate each pair.
        seg = {
            "start": 13.98,
            "end": 21.34,
            "text": "send file from Windows to Mac, Mac to iPhone, iPhone to Android",
            "words": [
                {"word": "send", "start": 14.0, "end": 14.3},
                {"word": "file", "start": 14.3, "end": 14.6},
                {"word": "from", "start": 14.6, "end": 14.9},
                {"word": "Windows", "start": 14.9, "end": 15.4},
                {"word": "to", "start": 15.4, "end": 15.6},
                {"word": "Mac,", "start": 15.6, "end": 16.1},
                {"word": "Mac", "start": 16.68, "end": 17.18},
                {"word": "to", "start": 17.18, "end": 17.4},
                {"word": "iPhone,", "start": 17.4, "end": 18.0},
                {"word": "iPhone", "start": 18.6, "end": 19.1},
                {"word": "to", "start": 19.1, "end": 19.3},
                {"word": "Android", "start": 19.3, "end": 20.0},
            ],
        }
        analysis = _analyze_segments([seg], 21.34, remove_silences=False, remove_fillers=False)
        self.assertEqual(
            [s for s in analysis["suggestions"] if s["kind"] == "repeat"], []
        )

    def test_tight_unpunctuated_repeat_is_still_a_stutter(self):
        seg = {
            "start": 0.0,
            "end": 2.0,
            "text": "the the cat sat",
            "words": [
                {"word": "the", "start": 0.0, "end": 0.2},
                {"word": "the", "start": 0.24, "end": 0.44},
                {"word": "cat", "start": 0.44, "end": 0.8},
                {"word": "sat", "start": 0.8, "end": 1.2},
            ],
        }
        analysis = _analyze_segments([seg], 2.0, remove_silences=False, remove_fillers=False)
        repeats = [s for s in analysis["suggestions"] if s["kind"] == "repeat"]
        self.assertEqual(len(repeats), 1)
        self.assertAlmostEqual(repeats[0]["start"], 0.0, places=3)
        self.assertAlmostEqual(repeats[0]["end"], 0.2, places=3)

    def test_truncated_word_is_a_false_start(self):
        seg = {
            "start": 0.0,
            "end": 2.0,
            "text": "I wen- I went to the store",
            "words": [
                {"word": "I", "start": 0.0, "end": 0.1},
                {"word": "wen-", "start": 0.1, "end": 0.4},
                {"word": "I", "start": 0.6, "end": 0.7},
                {"word": "went", "start": 0.7, "end": 0.9},
                {"word": "to", "start": 0.9, "end": 1.0},
                {"word": "the", "start": 1.0, "end": 1.2},
                {"word": "store", "start": 1.2, "end": 2.0},
            ],
        }
        analysis = _analyze_segments([seg], 2.0, remove_silences=False, remove_fillers=False)
        false_starts = [
            s for s in analysis["suggestions"] if s.get("title") == "False start"
        ]
        self.assertEqual(len(false_starts), 1)
        self.assertAlmostEqual(false_starts[0]["start"], 0.1, places=3)
        self.assertAlmostEqual(false_starts[0]["end"], 0.4, places=3)


class WeakFillerTests(unittest.TestCase):
    def _segment_with_gap_after_like(self, gap: float) -> dict:
        return {
            "start": 0.0,
            "end": 3.0 + gap,
            "text": "I like this one",
            "words": [
                {"word": "I", "start": 0.0, "end": 0.2},
                {"word": "like", "start": 0.2, "end": 0.5},
                {"word": "this", "start": 0.5 + gap, "end": 1.0 + gap},
                {"word": "one", "start": 1.0 + gap, "end": 1.5 + gap},
            ],
        }

    def test_weak_filler_mid_flow_is_not_flagged(self):
        analysis = _analyze_segments(
            [self._segment_with_gap_after_like(0.0)], 3.0, remove_silences=False
        )
        self.assertEqual(
            [s for s in analysis["suggestions"] if s["kind"] == "filler"], []
        )

    def test_weak_filler_detached_by_pause_is_flagged(self):
        analysis = _analyze_segments(
            [self._segment_with_gap_after_like(0.3)], 3.3, remove_silences=False
        )
        fillers = [s for s in analysis["suggestions"] if s["kind"] == "filler"]
        self.assertEqual(len(fillers), 1)
        self.assertEqual(fillers[0]["detail"], "like")

    def test_strong_filler_still_always_flagged(self):
        seg = {
            "start": 0.0,
            "end": 1.0,
            "text": "um hello",
            "words": [
                {"word": "um", "start": 0.0, "end": 0.3},
                {"word": "hello", "start": 0.3, "end": 1.0},
            ],
        }
        analysis = _analyze_segments([seg], 1.0, remove_silences=False)
        fillers = [s for s in analysis["suggestions"] if s["kind"] == "filler"]
        self.assertEqual(len(fillers), 1)
        self.assertEqual(fillers[0]["severity"], "high")

    def test_bigram_filler_detached_is_flagged_as_one_unit(self):
        seg = {
            "start": 0.0,
            "end": 3.0,
            "text": "it works you know the setup is easy",
            "words": [
                {"word": "it", "start": 0.0, "end": 0.2},
                {"word": "works", "start": 0.2, "end": 0.6},
                {"word": "you", "start": 0.9, "end": 1.05},
                {"word": "know", "start": 1.05, "end": 1.25},
                {"word": "the", "start": 1.6, "end": 1.7},
                {"word": "setup", "start": 1.7, "end": 2.1},
                {"word": "is", "start": 2.1, "end": 2.3},
                {"word": "easy", "start": 2.3, "end": 3.0},
            ],
        }
        analysis = _analyze_segments([seg], 3.0, remove_silences=False)
        fillers = [s for s in analysis["suggestions"] if s["kind"] == "filler"]
        self.assertEqual(len(fillers), 1)
        self.assertEqual(fillers[0]["detail"], "you know")
        self.assertAlmostEqual(fillers[0]["start"], 0.9 - 0.03, places=3)
        self.assertAlmostEqual(fillers[0]["end"], 1.25 + 0.03, places=3)


if __name__ == "__main__":
    unittest.main()
