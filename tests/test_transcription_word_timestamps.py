"""Task P2: word-level transcript timestamps.

Covers:
- app.jobs.transcription.transcribe_video: calls model.transcribe(...,
  word_timestamps=<env-gated bool>) and persists a `words: [{word,start,end}]`
  list per segment (rounded to 3 decimals, whitespace-stripped, no
  `probability` key) when faster-whisper returns word objects.
- The worker tolerates segments with words=None (omits the `words` key
  entirely rather than writing null/[]).
- WHISPER_WORD_TIMESTAMPS env gate (default "1"/on; "0" disables the flag
  passed to Whisper).
- app.services.auto_edit._analyze_segments: filler suggestion timing uses the
  segment's real word boundaries when present, and falls back to the old
  even-division estimate when a segment has no (or mismatched) word data.
- app.api.video_payload.transcription_to_dict / VideoTranscriptionNested:
  `words` flows through to the API payload untouched.

None of this loads a real Whisper model — faster-whisper's WhisperModel is
always mocked.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from app.api.video_payload import transcription_to_dict
from app.services.auto_edit import _analyze_segments


class _FakeQuery:
    def __init__(self, result):
        self._result = result

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def first(self):
        return self._result


class _FakeWord:
    """Mimics a faster-whisper Word namedtuple-like object (.word/.start/.end/.probability)."""

    def __init__(self, word: str, start: float, end: float, probability: float = 0.9):
        self.word = word
        self.start = start
        self.end = end
        self.probability = probability


class TranscribeVideoWorkerWordTimestampsTests(unittest.TestCase):
    """Worker: word_timestamps threaded into model.transcribe(); words[] persisted."""

    def _make_db(self, vt, video, job=None):
        from app.db.models import RepurposeJob, Video as VideoModel, VideoTranscription as VTModel

        db = mock.MagicMock()

        def query_side_effect(model, *a, **k):
            if model is VTModel:
                return _FakeQuery(vt)
            if model is VideoModel:
                return _FakeQuery(video)
            if model is RepurposeJob:
                return _FakeQuery(job)
            return _FakeQuery(None)

        db.query.side_effect = query_side_effect
        return db

    def _run_transcribe_video(self, *, segments, env=None):
        from app.jobs import transcription as job_mod

        vt = SimpleNamespace(
            status="pending",
            error_message=None,
            language=None,
            segments=None,
            speakers=None,
            speaker_count=None,
            model_name=None,
            detected_language=None,
        )
        video = SimpleNamespace(
            id=1,
            duration=10,
            file_path="https://example.test/audio-source.mp4",
            ingest_page_url=None,
        )
        db = self._make_db(vt, video)

        info = SimpleNamespace(language="en")
        fake_instance = mock.MagicMock()
        fake_instance.transcribe.return_value = (segments, info)
        fake_model_cls = mock.MagicMock(return_value=fake_instance)

        def fake_ffmpeg(input_src, wav_path, **kwargs):
            wav_path.write_bytes(b"\x00" * 20000)

        env = env or {}
        with mock.patch.object(job_mod, "SessionLocal", return_value=db), mock.patch.object(
            job_mod, "_run_ffmpeg_to_wav", side_effect=fake_ffmpeg
        ), mock.patch("faster_whisper.WhisperModel", fake_model_cls), mock.patch(
            "app.services.repurpose_pipeline.create_clips_for_completed_repurpose_jobs",
            return_value=None,
        ), mock.patch.dict("os.environ", env, clear=False):
            job_mod.transcribe_video(video.id, None)

        return vt, fake_instance

    def test_words_persisted_with_rounded_stripped_shape(self):
        seg = SimpleNamespace(
            start=0.0,
            end=1.5,
            text="hello world",
            words=[
                _FakeWord(" hello", 0.00012, 0.50006, probability=0.987),
                _FakeWord(" world ", 0.5001, 1.49999, probability=0.912),
            ],
        )
        vt, fake_instance = self._run_transcribe_video(segments=[seg])

        _, kwargs = fake_instance.transcribe.call_args
        self.assertIs(kwargs.get("word_timestamps"), True)

        self.assertEqual(len(vt.segments), 1)
        persisted = vt.segments[0]
        # Existing segment fields kept exactly.
        self.assertEqual(persisted["start"], 0.0)
        self.assertEqual(persisted["end"], 1.5)
        self.assertEqual(persisted["text"], "hello world")
        self.assertEqual(persisted["speaker"], "SPEAKER_1")

        self.assertIn("words", persisted)
        self.assertEqual(
            persisted["words"],
            [
                {"word": "hello", "start": 0.0, "end": 0.5},
                {"word": "world", "start": 0.5, "end": 1.5},
            ],
        )
        for w in persisted["words"]:
            self.assertNotIn("probability", w)

    def test_words_offset_by_range_start_matches_segment_offset(self):
        job = SimpleNamespace(
            video_id=1,
            source_mode="range",
            source_meta={
                "source_range_start_seconds": 10.0,
                "source_range_end_seconds": 20.0,
            },
            source_trim_seconds=None,
            source_url=None,
        )
        seg = SimpleNamespace(
            start=1.0,
            end=2.0,
            text="hi",
            words=[_FakeWord("hi", 1.0, 2.0)],
        )

        from app.jobs import transcription as job_mod

        vt = SimpleNamespace(
            status="pending",
            error_message=None,
            language=None,
            segments=None,
            speakers=None,
            speaker_count=None,
            model_name=None,
            detected_language=None,
        )
        video = SimpleNamespace(
            id=1,
            duration=30,
            file_path="https://example.test/audio-source.mp4",
            ingest_page_url=None,
        )
        db = self._make_db(vt, video, job=job)
        info = SimpleNamespace(language="en")
        fake_instance = mock.MagicMock()
        fake_instance.transcribe.return_value = ([seg], info)
        fake_model_cls = mock.MagicMock(return_value=fake_instance)

        def fake_ffmpeg(input_src, wav_path, **kwargs):
            wav_path.write_bytes(b"\x00" * 20000)

        with mock.patch.object(job_mod, "SessionLocal", return_value=db), mock.patch.object(
            job_mod, "_run_ffmpeg_to_wav", side_effect=fake_ffmpeg
        ), mock.patch("faster_whisper.WhisperModel", fake_model_cls), mock.patch(
            "app.services.repurpose_pipeline.create_clips_for_completed_repurpose_jobs",
            return_value=None,
        ):
            job_mod.transcribe_video(video.id, None)

        persisted = vt.segments[0]
        # range_start (10.0, from job.source_meta) is applied identically to
        # both the segment's own start/end and its words[] timings.
        self.assertEqual(persisted["start"], 11.0)
        self.assertEqual(persisted["end"], 12.0)
        self.assertEqual(persisted["words"][0]["start"], 11.0)
        self.assertEqual(persisted["words"][0]["end"], 12.0)

    def test_words_none_is_tolerated_and_key_omitted(self):
        seg = SimpleNamespace(start=0.0, end=1.0, text="ok", words=None)
        vt, _ = self._run_transcribe_video(segments=[seg])

        self.assertEqual(len(vt.segments), 1)
        self.assertNotIn("words", vt.segments[0])
        # Other fields are unaffected.
        self.assertEqual(vt.segments[0]["text"], "ok")

    def test_segment_missing_words_attribute_entirely_is_tolerated(self):
        # Some fake/legacy segment shapes may not even have a `.words` attr.
        seg = SimpleNamespace(start=0.0, end=1.0, text="ok")
        vt, _ = self._run_transcribe_video(segments=[seg])
        self.assertNotIn("words", vt.segments[0])

    def test_empty_words_list_is_tolerated_and_key_omitted(self):
        seg = SimpleNamespace(start=0.0, end=1.0, text="ok", words=[])
        vt, _ = self._run_transcribe_video(segments=[seg])
        self.assertNotIn("words", vt.segments[0])

    def test_env_flag_default_on(self):
        seg = SimpleNamespace(start=0.0, end=1.0, text="ok", words=[])
        _, fake_instance = self._run_transcribe_video(segments=[seg], env={})
        _, kwargs = fake_instance.transcribe.call_args
        self.assertIs(kwargs.get("word_timestamps"), True)

    def test_env_flag_disabled_via_zero(self):
        seg = SimpleNamespace(start=0.0, end=1.0, text="ok", words=[])
        _, fake_instance = self._run_transcribe_video(
            segments=[seg], env={"WHISPER_WORD_TIMESTAMPS": "0"}
        )
        _, kwargs = fake_instance.transcribe.call_args
        self.assertIs(kwargs.get("word_timestamps"), False)


class AnalyzeSegmentsRealWordBoundaryTests(unittest.TestCase):
    """Filler suggestion timing uses segment words[] when present."""

    def test_uses_real_word_times_when_present(self):
        # Even-division over "hello um there we go" (5 words) across a 2.0s
        # segment would place "um" (index 1) at [0.4, 0.8). The real Whisper
        # word timing below places it much later, at [1.10, 1.35) — a gap
        # large enough that the two approaches are unambiguously distinguishable.
        segments = [
            {
                "start": 0.0,
                "end": 2.0,
                "text": "hello um there we go",
                "words": [
                    {"word": "hello", "start": 0.0, "end": 0.35},
                    {"word": "um", "start": 1.10, "end": 1.35},
                    {"word": "there", "start": 1.35, "end": 1.55},
                    {"word": "we", "start": 1.55, "end": 1.75},
                    {"word": "go", "start": 1.75, "end": 2.0},
                ],
            }
        ]
        analysis = _analyze_segments(segments, duration=2.0)
        filler = next(s for s in analysis["suggestions"] if s["kind"] == "filler")

        # ws/we get a +/-0.03 pad applied around the raw word boundary.
        self.assertAlmostEqual(filler["start"], 1.10 - 0.03, places=3)
        self.assertAlmostEqual(filler["end"], 1.35 + 0.03, places=3)

        # Sanity: this must differ measurably from the even-division estimate
        # (span = 2.0/5 = 0.4s per word; "um" is word index 1 => ws=0.4).
        even_division_start = max(0, 0.0 + 1 * 0.4 - 0.03)
        self.assertGreater(abs(filler["start"] - even_division_start), 0.5)

    def test_falls_back_to_char_proportional_when_words_absent(self):
        segments = [
            {"start": 0.0, "end": 2.0, "text": "hello um there we go"},
        ]
        analysis = _analyze_segments(segments, duration=2.0)
        filler = next(s for s in analysis["suggestions"] if s["kind"] == "filler")

        # Without word timestamps the estimate is character-proportional
        # interpolation across the segment: "hello um there we go" has 16
        # letters, "um" spans chars [5, 7) => [0.625, 0.875) of 2.0s.
        self.assertAlmostEqual(filler["start"], 2.0 * (5 / 16) - 0.03, places=3)
        self.assertAlmostEqual(filler["end"], 2.0 * (7 / 16) + 0.03, places=3)

    def test_count_mismatch_recovers_timing_via_alignment(self):
        # words[] has 3 entries but the segment text tokenizes to 5 words.
        # The old guard threw all timings away; fuzzy alignment instead
        # recognizes "hello um there" as the concatenation of three tokens
        # and splits its real [0.0, 1.0] span among them proportionally.
        segments = [
            {
                "start": 0.0,
                "end": 2.0,
                "text": "hello um there we go",
                "words": [
                    {"word": "hello um there", "start": 0.0, "end": 1.0},
                    {"word": "we", "start": 1.0, "end": 1.5},
                    {"word": "go", "start": 1.5, "end": 2.0},
                ],
            }
        ]
        analysis = _analyze_segments(segments, duration=2.0)
        filler = next(s for s in analysis["suggestions"] if s["kind"] == "filler")

        # "um" is chars [5, 7) of the 12-letter merged word spanning [0, 1.0]:
        self.assertAlmostEqual(filler["start"], 1.0 * (5 / 12) - 0.03, places=3)
        self.assertAlmostEqual(filler["end"], 1.0 * (7 / 12) + 0.03, places=3)

    def test_malformed_word_entries_fall_back_cleanly(self):
        segments = [
            {
                "start": 0.0,
                "end": 2.0,
                "text": "hello um there we go",
                "words": [
                    {"word": "hello", "start": 0.0, "end": 0.35},
                    {"word": "um", "start": "not-a-number", "end": 1.35},
                    {"word": "there", "start": 1.35, "end": 1.55},
                    {"word": "we", "start": 1.55, "end": 1.75},
                    {"word": "go", "start": 1.75, "end": 2.0},
                ],
            }
        ]
        # Should not raise; the poisoned words list is rejected whole and the
        # estimate degrades to character-proportional interpolation.
        analysis = _analyze_segments(segments, duration=2.0)
        filler = next(s for s in analysis["suggestions"] if s["kind"] == "filler")
        self.assertAlmostEqual(filler["start"], 2.0 * (5 / 16) - 0.03, places=3)
        self.assertAlmostEqual(filler["end"], 2.0 * (7 / 16) + 0.03, places=3)


class TranscriptionPayloadWordsPassthroughTests(unittest.TestCase):
    """API payload surfaces `words` without truncation."""

    def test_transcription_to_dict_preserves_words(self):
        tr = SimpleNamespace(
            status="completed",
            segments=[
                {
                    "start": 0.0,
                    "end": 1.0,
                    "text": "hi",
                    "speaker": "SPEAKER_1",
                    "words": [{"word": "hi", "start": 0.0, "end": 1.0}],
                }
            ],
            speakers=["SPEAKER_1"],
            speaker_count=1,
            error_message=None,
            updated_at=None,
            language=None,
            detected_language="en",
        )
        payload = transcription_to_dict(tr)
        self.assertEqual(
            payload["segments"][0]["words"],
            [{"word": "hi", "start": 0.0, "end": 1.0}],
        )

    def test_video_transcription_nested_schema_preserves_words(self):
        from app.api.models.videos import VideoTranscriptionNested

        model = VideoTranscriptionNested(
            status="completed",
            segments=[
                {
                    "start": 0.0,
                    "end": 1.0,
                    "text": "hi",
                    "speaker": "SPEAKER_1",
                    "words": [{"word": "hi", "start": 0.0, "end": 1.0}],
                }
            ],
        )
        dumped = model.dict()
        self.assertEqual(
            dumped["segments"][0]["words"],
            [{"word": "hi", "start": 0.0, "end": 1.0}],
        )


if __name__ == "__main__":
    unittest.main()
