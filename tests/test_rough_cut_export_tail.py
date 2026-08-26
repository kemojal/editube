"""Rendering the part of the timeline that comes after the A-roll.

The base MP4 is built from the source video's own frames, so it stops where
they do. A clip appended after the A-roll therefore had nothing underneath it
and was composited onto nothing — it simply did not appear in the export. This
covers the base being grown to hold it, and the clip's own sound arriving with
it (a quote picked for what is said in it that exports silent is not exported).
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import ActivityFeed, AiResult, Project, User, Video, VideoTranscription
from app.jobs import rough_cut_export as rce


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(type_, compiler, **kw):
    return "JSON"


class RoughCutExportTailTests(unittest.TestCase):
    """Job-level: ffmpeg is recorded rather than run."""

    def setUp(self) -> None:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(
            engine,
            tables=[
                User.__table__,
                Project.__table__,
                Video.__table__,
                VideoTranscription.__table__,
                AiResult.__table__,
                ActivityFeed.__table__,
            ],
        )
        self.db = sessionmaker(bind=engine)()

        self.user = User(email="editor@example.com", name="Edna", role="creator")
        self.db.add(self.user)
        self.db.flush()
        self.project = Project(name="Launch", creator_id=self.user.id, workspace_id=1)
        self.db.add(self.project)
        self.db.flush()

        self.video = Video(
            project_id=self.project.id,
            name="Interview",
            version=1,
            file_path="/media/interview.mp4",
            uploader_id=self.user.id,
        )
        self.sibling = Video(
            project_id=self.project.id,
            name="Answer",
            version=1,
            file_path="/media/answer.mp4",
            uploader_id=self.user.id,
        )
        self.db.add_all([self.video, self.sibling])
        self.db.commit()

        for target, value in (
            ("app.jobs.rough_cut_export.SessionLocal", self.db),
            ("app.jobs.rough_cut_export._ffprobe_has_video", True),
            ("app.jobs.rough_cut_export._ffprobe_has_audio", True),
            ("app.jobs.rough_cut_export._ffprobe_avg_frame_rate", "30/1"),
            ("app.jobs.rough_cut_export._ffprobe_duration", 20.0),
            ("app.jobs.rough_cut_export.cloudinary_credentials_configured", True),
            (
                "app.jobs.rough_cut_export.upload_local_path_to_cloudinary",
                "https://cdn.example.test/out.mp4",
            ),
        ):
            patcher = mock.patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

        close_patcher = mock.patch.object(self.db, "close", lambda: None)
        close_patcher.start()
        self.addCleanup(close_patcher.stop)

        self.commands: list[list[str]] = []

        def _record(args):
            self.commands.append(list(args))
            Path(args[-1]).write_bytes(b"0" * 128)

        run_patcher = mock.patch("app.jobs.rough_cut_export._run_ffmpeg", side_effect=_record)
        run_patcher.start()
        self.addCleanup(run_patcher.stop)

    def tearDown(self) -> None:
        self.db.close()

    def _run(self, layers, keep_ranges=((0, 20),)):
        row = AiResult(
            video_id=self.video.id,
            result_type="rough_cut_export",
            status="queued",
            result_data={
                "format": "mp4",
                "keepRanges": [{"start": s, "end": e} for s, e in keep_ranges],
                "exportSettings": {},
                "timelineLayers": layers,
            },
        )
        self.db.add(row)
        self.db.commit()
        rce.rough_cut_export_job(row.id)
        self.db.refresh(row)
        return row

    def _graphs(self) -> list[str]:
        return [
            command[command.index("-filter_complex") + 1]
            for command in self.commands
            if "-filter_complex" in command
        ]

    def _layer(self, **overrides):
        value = {
            "id": "tail-1",
            "clipKey": "media:tail-1",
            "kind": "video",
            "videoId": self.sibling.id,
            "start": 20,
            "end": 26,
            "sourceStart": 3,
            "trackOrder": 0,
            "aboveText": True,
            "audioEnabled": True,
            "settings": {},
        }
        value.update(overrides)
        return value

    def test_an_appended_clip_grows_the_base_and_is_composited_onto_it(self) -> None:
        row = self._run([self._layer()])

        self.assertEqual(row.status, "completed")
        self.assertEqual(row.result_data.get("timelineLayerChunks"), 1)

        graphs = self._graphs()
        tail = [graph for graph in graphs if "tpad=" in graph]
        self.assertEqual(len(tail), 1, "the base is extended exactly once")
        # 20s of A-roll kept, clip runs to 26 — six seconds of tail.
        self.assertIn("stop_duration=6.000000", tail[0])
        self.assertIn("apad=pad_dur=6.000000", tail[0])

        composited = [graph for graph in graphs if "overlay=" in graph]
        self.assertTrue(composited, "the appended clip is composited over the base")

    def test_the_appended_clip_is_cut_from_its_own_video_at_its_own_offset(self) -> None:
        self._run([self._layer()])

        layered = [
            command
            for command in self.commands
            if "-filter_complex" in command
            and "overlay=" in command[command.index("-filter_complex") + 1]
        ][-1]

        self.assertIn("/media/answer.mp4", layered)
        # sourceStart 3 with no offset into the clip.
        self.assertIn("3.000000", layered)

    def test_the_appended_clip_brings_its_sound(self) -> None:
        self._run([self._layer()])

        graphs = [graph for graph in self._graphs() if "overlay=" in graph]
        self.assertTrue(any("amix=inputs=2" in graph for graph in graphs))
        # The clip starts where the A-roll ends, so its audio is delayed 20s.
        self.assertTrue(any("adelay=delays=20000:all=1" in graph for graph in graphs))

    def test_a_muted_appended_clip_leaves_the_base_audio_alone(self) -> None:
        self._run([self._layer(audioEnabled=False)])

        graphs = [graph for graph in self._graphs() if "overlay=" in graph]
        self.assertTrue(graphs)
        self.assertFalse(any("amix=" in graph for graph in graphs))

    def test_nothing_appended_means_no_extension_at_all(self) -> None:
        self._run([self._layer(start=2, end=8)])

        self.assertFalse([graph for graph in self._graphs() if "tpad=" in graph])

    def _srt_written(self) -> str | None:
        for command in self.commands:
            if "-vf" in command and str(command[command.index("-vf") + 1]).startswith("subtitles="):
                return command[command.index("-vf") + 1]
        return None

    def test_an_appended_clip_is_captioned_from_its_own_transcription(self) -> None:
        self.db.add(
            VideoTranscription(
                video_id=self.sibling.id,
                status="completed",
                segments=[
                    {"start": 0, "end": 2, "text": "before the clip starts"},
                    {"start": 3, "end": 6, "text": "the part that was picked"},
                    {"start": 40, "end": 42, "text": "long after it ends"},
                ],
            )
        )
        self.db.commit()

        written: list[list[tuple[float, float, str]]] = []
        with mock.patch.object(
            rce, "_write_srt", side_effect=lambda path, entries: written.append(list(entries))
        ):
            row = self._run([self._layer()])

        self.assertEqual(row.status, "completed")
        self.assertEqual(len(written), 1)
        # The clip plays source 3..9 and sits at output 20..26, so only the
        # middle cue survives, shifted to where the clip actually is.
        self.assertEqual(written[0], [(20.0, 23.0, "the part that was picked")])
        self.assertIsNotNone(self._srt_written())

    def test_a_muted_clip_is_not_captioned(self) -> None:
        self.db.add(
            VideoTranscription(
                video_id=self.sibling.id,
                status="completed",
                segments=[{"start": 3, "end": 6, "text": "nobody can hear this"}],
            )
        )
        self.db.commit()

        written: list[list[tuple[float, float, str]]] = []
        with mock.patch.object(
            rce, "_write_srt", side_effect=lambda path, entries: written.append(list(entries))
        ):
            self._run([self._layer(audioEnabled=False)])

        self.assertEqual(written, [])

    def test_an_audible_clip_takes_the_caption_span_from_the_a_roll(self) -> None:
        self.db.add_all(
            [
                VideoTranscription(
                    video_id=self.video.id,
                    status="completed",
                    segments=[{"start": 0, "end": 20, "text": "the interview"}],
                ),
                VideoTranscription(
                    video_id=self.sibling.id,
                    status="completed",
                    segments=[{"start": 0, "end": 4, "text": "the inserted answer"}],
                ),
            ]
        )
        self.db.commit()

        written: list[list[tuple[float, float, str]]] = []
        with mock.patch.object(
            rce, "_write_srt", side_effect=lambda path, entries: written.append(list(entries))
        ):
            # Placed over the middle of the A-roll rather than after it.
            self._run([self._layer(start=8, end=12, sourceStart=0)])

        self.assertEqual(
            written[0],
            [
                (0.0, 8.0, "the interview"),
                (8.0, 12.0, "the inserted answer"),
                (12.0, 20.0, "the interview"),
            ],
        )

    def test_a_clip_from_outside_the_project_never_reaches_ffmpeg(self) -> None:
        outsider_project = Project(name="Theirs", creator_id=self.user.id, workspace_id=2)
        self.db.add(outsider_project)
        self.db.flush()
        outsider = Video(
            project_id=outsider_project.id,
            name="Secret",
            version=1,
            file_path="/media/secret.mp4",
            uploader_id=self.user.id,
        )
        self.db.add(outsider)
        self.db.commit()

        row = self._run([self._layer(videoId=outsider.id)])

        self.assertEqual(row.result_data.get("timelineLayerChunks"), 0)
        self.assertNotIn("/media/secret.mp4", " ".join(" ".join(c) for c in self.commands))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
