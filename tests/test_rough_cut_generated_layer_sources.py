"""AI-generated media as a timeline layer source.

Generated media lives in `generated_media`, not `videos`, so it has no
`video_id` at all. The export used to read that absence as "the primary video"
and composite the A-roll in its place — the B-roll was never missing, it was
silently replaced by a second copy of the interview, which looks like a render
bug rather than a resolution bug and is invisible in the editor's own preview.
Stills fared worse: layers whose `kind` was not `"video"` were dropped outright,
so a generated image never reached the render at all.

These tests pin both: a generated layer resolves to its own media, and a still
survives to the graph. The security property is unchanged — the client sends an
id, the worker resolves the path, and a row outside the project resolves to
nothing.
"""

from __future__ import annotations

import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import AiResult, GeneratedMedia, Project, User, Video
from app.jobs import rough_cut_export as rce


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(type_, compiler, **kw):
    return "JSON"


def _layer(**overrides):
    value = {
        "id": "clip-1",
        "clipKey": "media:clip-1",
        "kind": "video",
        "start": 0,
        "end": 4,
        "sourceStart": 0,
        "trackOrder": 0,
        "aboveText": True,
        "settings": {},
    }
    value.update(overrides)
    return value


class GeneratedLayerSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(
            engine,
            tables=[
                User.__table__,
                Project.__table__,
                Video.__table__,
                AiResult.__table__,
                GeneratedMedia.__table__,
            ],
        )
        self.db = sessionmaker(bind=engine)()

        self.user = User(email="editor@example.com", name="Edna", role="creator")
        self.db.add(self.user)
        self.db.flush()

        self.project = Project(name="Launch", creator_id=self.user.id, workspace_id=1)
        self.other_project = Project(name="Someone else", creator_id=self.user.id, workspace_id=2)
        self.db.add_all([self.project, self.other_project])
        self.db.flush()

        self.primary = Video(
            project_id=self.project.id,
            name="Interview",
            version=1,
            file_path="/media/interview.mp4",
            uploader_id=self.user.id,
        )
        self.db.add(self.primary)
        self.db.flush()

        def _generated(project, kind, url, status="ready"):
            row = GeneratedMedia(
                project_id=project.id,
                user_id=self.user.id,
                kind=kind,
                prompt="a cluttered desk at dusk",
                status=status,
                url=url,
            )
            self.db.add(row)
            self.db.flush()
            return row

        self.still = _generated(self.project, "image", "https://cdn.test/desk.png")
        self.motion = _generated(self.project, "video", "https://cdn.test/desk.mp4")
        self.pending = _generated(self.project, "image", None, status="running")
        self.outsider = _generated(self.other_project, "image", "https://cdn.test/theirs.png")
        self.db.commit()

        patcher_duration = mock.patch.object(rce, "_ffprobe_duration", return_value=60.0)
        patcher_audio = mock.patch.object(rce, "_ffprobe_has_audio", return_value=True)
        self.addCleanup(patcher_duration.stop)
        self.addCleanup(patcher_audio.stop)
        patcher_duration.start()
        patcher_audio.start()

    def tearDown(self) -> None:
        self.db.close()

    def _approve(self, requested):
        return rce._approved_timeline_layers(
            self.db,
            self.primary.id,
            requested,
            media_src=self.primary.file_path,
            source_duration=60.0,
        )

    # -- the bug this file exists for ---------------------------------------

    def test_a_generated_clip_is_cut_from_its_own_media_not_the_primary(self) -> None:
        """The G2 regression. Before the fix this returned the interview."""
        layers = self._approve(
            [
                _layer(
                    id="broll",
                    clipKey="media:broll",
                    kind="image",
                    sourceKind="generated",
                    sourceId=self.still.id,
                )
            ]
        )

        self.assertEqual(len(layers), 1)
        self.assertEqual(layers[0]["source"], "https://cdn.test/desk.png")
        self.assertNotEqual(layers[0]["source"], self.primary.file_path)

    def test_an_image_layer_survives_normalization(self) -> None:
        """`kind != "video"` used to be dropped, so stills never rendered."""
        layers = rce._normalize_timeline_layers(
            [_layer(kind="image", sourceKind="generated", sourceId=self.still.id)],
            video_id=self.primary.id,
        )
        self.assertEqual(len(layers), 1)
        self.assertEqual(layers[0]["kind"], "image")

    def test_an_audio_lane_layer_survives_normalization(self) -> None:
        """Music/SFX dropped on an audio lane has no picture but must render.

        It used to be filtered out with the unknown kinds, so a track the
        editor was playing was silent in every export.
        """
        layers = rce._normalize_timeline_layers(
            [_layer(kind="audio")], video_id=self.primary.id
        )
        self.assertEqual(len(layers), 1)
        self.assertEqual(layers[0]["kind"], "audio")

    def test_an_unknown_layer_kind_is_still_dropped(self) -> None:
        layers = rce._normalize_timeline_layers(
            [_layer(kind="caption")], video_id=self.primary.id
        )
        self.assertEqual(layers, [])

    # -- authorization -------------------------------------------------------

    def test_generated_media_from_another_project_is_dropped(self) -> None:
        layers = self._approve(
            [_layer(id="theirs", clipKey="media:theirs", kind="image",
                    sourceKind="generated", sourceId=self.outsider.id)]
        )
        self.assertEqual(layers, [])

    def test_an_unfinished_generation_is_dropped(self) -> None:
        """A `running` row has no bytes yet; compositing it would fail late."""
        layers = self._approve(
            [_layer(id="pending", clipKey="media:pending", kind="image",
                    sourceKind="generated", sourceId=self.pending.id)]
        )
        self.assertEqual(layers, [])

    def test_an_unknown_generated_id_is_dropped(self) -> None:
        layers = self._approve(
            [_layer(id="ghost", clipKey="media:ghost", kind="image",
                    sourceKind="generated", sourceId=999_999)]
        )
        self.assertEqual(layers, [])

    def test_the_payload_can_never_name_its_own_generated_source(self) -> None:
        layers = self._approve(
            [
                _layer(
                    id="evil",
                    clipKey="media:evil",
                    kind="image",
                    sourceKind="generated",
                    sourceId=self.still.id,
                    source="file:///etc/passwd",
                    sourceUrl="https://example.test/evil.mp4",
                )
            ]
        )
        self.assertEqual([layer["source"] for layer in layers], ["https://cdn.test/desk.png"])

    def test_a_generated_clip_cannot_claim_an_effect(self) -> None:
        """Effects are keyed by `video_id`; a generated row has none to match."""
        row = AiResult(
            video_id=self.primary.id,
            result_type="rough_cut_effect",
            status="completed",
            result_data={
                "effectType": "remove_bg",
                "clipKey": "media:broll",
                "outputUrl": "https://example.test/cutout.mp4",
                "clipTarget": {"start": 0, "end": 4},
            },
        )
        self.db.add(row)
        self.db.commit()

        layers = self._approve(
            [
                _layer(
                    id="broll",
                    clipKey="media:broll",
                    kind="image",
                    sourceKind="generated",
                    sourceId=self.still.id,
                    processedResultId=row.id,
                    processedEffectType="remove_bg",
                )
            ]
        )

        self.assertEqual(len(layers), 1)
        self.assertFalse(layers[0]["processed"])
        self.assertEqual(layers[0]["source"], "https://cdn.test/desk.png")

    # -- back-compat ---------------------------------------------------------

    def test_a_legacy_layer_without_a_discriminator_still_means_the_primary(self) -> None:
        layers = self._approve([_layer(id="legacy", clipKey="media:legacy")])
        self.assertEqual([layer["source"] for layer in layers], ["/media/interview.mp4"])

    def test_a_legacy_layer_with_only_a_video_id_resolves_as_a_video(self) -> None:
        layers = rce._normalize_timeline_layers(
            [_layer(videoId=self.primary.id)], video_id=self.primary.id
        )
        self.assertEqual(layers[0]["sourceRef"], ("video", self.primary.id))

    # -- stills have no duration to be trimmed against -----------------------

    def test_a_still_is_not_trimmed_against_a_probed_duration(self) -> None:
        """A still plays for exactly as long as the clip asks.

        Bounding it against ffprobe's answer (which for a PNG is either absent
        or a nominal fraction of a second) would collapse every generated image
        to a sliver.
        """
        with mock.patch.object(rce, "_ffprobe_duration", return_value=0.04):
            layers = self._approve(
                [
                    _layer(
                        id="still",
                        clipKey="media:still",
                        kind="image",
                        sourceKind="generated",
                        sourceId=self.still.id,
                        start=10,
                        end=15,
                    )
                ]
            )

        self.assertEqual(len(layers), 1)
        self.assertEqual(layers[0]["start"], 10)
        self.assertEqual(layers[0]["end"], 15)
        self.assertTrue(layers[0]["isStill"])

    def test_a_generated_motion_clip_is_bounded_like_any_other_video(self) -> None:
        with mock.patch.object(
            rce,
            "_ffprobe_duration",
            side_effect=lambda path: 60.0 if path == "/media/interview.mp4" else 5.0,
        ):
            layers = self._approve(
                [
                    _layer(
                        id="motion",
                        clipKey="media:motion",
                        sourceKind="generated",
                        sourceId=self.motion.id,
                        start=100,
                        end=130,
                        sourceStart=2,
                    )
                ]
            )

        self.assertEqual(len(layers), 1)
        self.assertFalse(layers[0]["isStill"])
        self.assertEqual(layers[0]["end"], 103)

    def test_a_still_never_joins_the_audio_mix(self) -> None:
        layers = self._approve(
            [
                _layer(
                    id="still",
                    clipKey="media:still",
                    kind="image",
                    sourceKind="generated",
                    sourceId=self.still.id,
                    audioEnabled=True,
                )
            ]
        )
        self.assertFalse(layers[0]["hasAudio"])


class StillInputCommandTests(unittest.TestCase):
    """A still needs `-loop 1 -t`; seeking into one yields nothing."""

    def _chunk(self, **overrides):
        value = {
            "id": "c",
            "clipKey": "media:c",
            "source": "/media/desk.png",
            "sourceSeek": 0.0,
            "clipOffset": 0.0,
            "outputStart": 1.0,
            "outputEnd": 5.0,
            "start": 1.0,
            "end": 5.0,
            "trackOrder": 0,
            "aboveText": True,
            "audioEnabled": False,
            "hasAudio": False,
            "isStill": True,
            "settings": {},
        }
        value.update(overrides)
        return value

    def _command(self, chunk):
        return rce._timeline_layers_command(
            base_video=rce.Path("/tmp/base.mp4"),
            chunks=[chunk],
            scale_w=1920,
            scale_h=1080,
            frame_rate=30.0,
            crf=23,
            output=rce.Path("/tmp/out.mp4"),
        )

    def test_a_still_input_loops_for_the_clip_length(self) -> None:
        command = self._command(self._chunk())
        self.assertIn("-loop", command)
        self.assertEqual(command[command.index("-loop") + 1], "1")
        # Pinned to the timeline's rate, not ffmpeg's 25fps image default.
        self.assertEqual(command[command.index("-framerate") + 1], "30.000000")
        self.assertEqual(command[command.index("-t") + 1], "4.000000")
        self.assertNotIn("-ss", command)

    def test_a_video_input_still_seeks(self) -> None:
        command = self._command(
            self._chunk(source="/media/desk.mp4", isStill=False, sourceSeek=2.5)
        )
        self.assertNotIn("-loop", command)
        self.assertEqual(command[command.index("-ss") + 1], "2.500000")
        self.assertEqual(command[command.index("-t") + 1], "4.000000")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
