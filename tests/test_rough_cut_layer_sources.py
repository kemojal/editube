"""Which videos a timeline layer may actually be cut from.

Layers used to be restricted to the video being exported, which silently threw
away every B-roll clip and every transcript-selected quote taken from another
clip in the bin. They are allowed now — but only from the same project, and
only resolved from the database by id, because the alternative (trusting a path
from the draft) turns the export worker into an arbitrary file reader.
"""

from __future__ import annotations

import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import AiResult, Project, User, Video
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


class LayerSourceAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(
            engine,
            tables=[
                User.__table__,
                Project.__table__,
                Video.__table__,
                AiResult.__table__,
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

        def _video(project, name, path):
            row = Video(
                project_id=project.id,
                name=name,
                version=1,
                file_path=path,
                uploader_id=self.user.id,
            )
            self.db.add(row)
            self.db.flush()
            return row

        self.primary = _video(self.project, "Interview", "/media/interview.mp4")
        self.sibling = _video(self.project, "B-roll", "/media/broll.mp4")
        self.outsider = _video(self.other_project, "Not yours", "/media/secret.mp4")
        self.db.commit()

        # ffprobe is not available (and not the subject) — every source reports
        # the same length and a soundtrack.
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

    def test_a_clip_from_another_video_in_the_project_is_cut_from_that_video(self) -> None:
        layers = self._approve(
            [
                _layer(id="own", clipKey="media:own", videoId=self.primary.id),
                _layer(id="broll", clipKey="media:broll", videoId=self.sibling.id),
            ]
        )

        by_id = {layer["id"]: layer for layer in layers}
        self.assertEqual(set(by_id), {"own", "broll"})
        self.assertEqual(by_id["own"]["source"], "/media/interview.mp4")
        self.assertEqual(by_id["broll"]["source"], "/media/broll.mp4")

    def test_a_video_from_another_project_is_dropped(self) -> None:
        layers = self._approve([_layer(id="theirs", clipKey="media:theirs", videoId=self.outsider.id)])
        self.assertEqual(layers, [])

    def test_an_unknown_video_id_is_dropped(self) -> None:
        layers = self._approve([_layer(id="ghost", clipKey="media:ghost", videoId=999_999)])
        self.assertEqual(layers, [])

    def test_a_layer_without_a_video_id_still_means_the_primary(self) -> None:
        layers = self._approve([_layer(id="legacy", clipKey="media:legacy")])
        self.assertEqual([layer["source"] for layer in layers], ["/media/interview.mp4"])

    def test_the_payload_can_never_name_its_own_source(self) -> None:
        layers = self._approve(
            [
                _layer(
                    id="evil",
                    clipKey="media:evil",
                    videoId=self.primary.id,
                    source="file:///etc/passwd",
                    sourceUrl="https://example.test/evil.mp4",
                )
            ]
        )
        self.assertEqual([layer["source"] for layer in layers], ["/media/interview.mp4"])

    def test_a_clip_is_bounded_by_its_own_media_not_the_primary(self) -> None:
        with mock.patch.object(
            rce,
            "_ffprobe_duration",
            side_effect=lambda path: 60.0 if path == "/media/interview.mp4" else 5.0,
        ):
            layers = self._approve(
                [
                    _layer(
                        id="short",
                        clipKey="media:short",
                        videoId=self.sibling.id,
                        start=100,
                        end=130,
                        sourceStart=2,
                    )
                ]
            )

        # Only 3s of the sibling remains after its own sourceStart, so the clip
        # cannot claim thirty. The primary's 60s says nothing about it.
        self.assertEqual(len(layers), 1)
        self.assertEqual(layers[0]["start"], 100)
        self.assertEqual(layers[0]["end"], 103)

    def test_an_effect_rendered_on_a_different_clip_is_not_applied(self) -> None:
        row = AiResult(
            video_id=self.sibling.id,
            result_type="rough_cut_effect",
            status="completed",
            result_data={
                "effectType": "remove_bg",
                "clipKey": "media:own",
                "outputUrl": "https://example.test/cutout.mp4",
                "clipTarget": {"start": 0, "end": 4},
            },
        )
        self.db.add(row)
        self.db.commit()

        # The layer belongs to the primary; the completed effect belongs to the
        # sibling. Matching clipKeys must not be enough to borrow it.
        layers = self._approve(
            [
                _layer(
                    id="own",
                    clipKey="media:own",
                    videoId=self.primary.id,
                    processedResultId=row.id,
                    processedEffectType="remove_bg",
                )
            ]
        )

        self.assertEqual(len(layers), 1)
        self.assertFalse(layers[0]["processed"])
        self.assertEqual(layers[0]["source"], "/media/interview.mp4")

    def test_an_effect_on_a_sibling_clip_is_applied_to_that_clip(self) -> None:
        row = AiResult(
            video_id=self.sibling.id,
            result_type="rough_cut_effect",
            status="completed",
            result_data={
                "effectType": "remove_bg",
                "clipKey": "media:broll",
                "outputUrl": "/media/broll-cutout.mp4",
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
                    videoId=self.sibling.id,
                    processedResultId=row.id,
                    processedEffectType="remove_bg",
                )
            ]
        )

        self.assertEqual(len(layers), 1)
        self.assertTrue(layers[0]["processed"])
        self.assertEqual(layers[0]["source"], "/media/broll-cutout.mp4")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
