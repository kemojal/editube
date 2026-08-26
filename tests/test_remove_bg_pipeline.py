from __future__ import annotations

import tempfile
import unittest
import sys
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace
from unittest import mock

from app.jobs.rough_cut_effect import (
    _completed_effect_source,
    _publish_output,
    build_chroma_key_command,
)
from app.jobs.rough_cut_export import (
    _approved_audio_ranges,
    _approved_processed_ranges,
    _masked_filter_complex,
    _video_segment_command,
)
from app.services.segmentation.chroma_matte import (
    chroma_keep_matte,
    combine_keep_mattes,
)
from app.services.segmentation.base import module_available


class _Query:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return self.rows


class RemoveBgExportContractTests(unittest.TestCase):
    def test_capability_check_accepts_an_already_loaded_module_without_a_spec(self):
        shim = ModuleType("remove_bg_test_shim")
        shim.__spec__ = None
        with mock.patch.dict(sys.modules, {"remove_bg_test_shim": shim}):
            self.assertTrue(module_available("remove_bg_test_shim"))

    def test_capability_check_rejects_a_missing_module_without_raising(self):
        with mock.patch("importlib.util.find_spec", side_effect=ValueError("broken spec")):
            self.assertFalse(module_available("remove_bg_missing_dependency"))

    def test_chroma_cutout_does_not_mux_incompatible_source_audio(self):
        command = build_chroma_key_command(
            "source.mp4",
            "cutout.webm",
            {"start": 0, "end": 1},
            {"chromaKey": True, "keyColor": "#00ff00"},
        )
        self.assertIn("-an", command)
        self.assertNotIn("copy", command)

    def test_chroma_matte_keys_selected_colour_and_preserves_foreground(self):
        import numpy as np

        # OpenCV frame order is BGR: green should key out, red should remain.
        frame = np.asarray([[[0, 255, 0], [0, 0, 255]]], dtype=np.uint8)
        matte = chroma_keep_matte(
            frame,
            {
                "chromaKey": True,
                "keyColor": "#00ff00",
                "similarity": 0.2,
                "blend": 0.1,
            },
        )
        self.assertEqual(int(matte[0, 0]), 0)
        self.assertEqual(int(matte[0, 1]), 255)

    def test_stacked_ai_and_chroma_mattes_multiply_soft_alpha(self):
        import numpy as np

        combined = combine_keep_mattes(
            np.asarray([[128]], dtype=np.uint8),
            np.asarray([[128]], dtype=np.uint8),
        )
        self.assertEqual(int(combined[0, 0]), 64)

    def test_processed_video_uses_original_only_for_audio(self):
        command = _video_segment_command(
            video_source="cutout.webm",
            audio_source="source.mp4",
            source_start=4.0,
            duration=2.0,
            vf="format=rgba,scale=64:64",
            scale_w=64,
            scale_h=64,
            crf=23,
            output=Path("out.mp4"),
            processed=True,
        )
        self.assertEqual(
            command[:8],
            ["ffmpeg", "-y", "-i", "cutout.webm", "-ss", "4.0", "-i", "source.mp4"],
        )
        self.assertIn("1:a?", command)
        self.assertIn("[bg][cutout]overlay=shortest=1[v]", " ".join(command))

    def test_enhanced_audio_uses_original_only_for_video(self):
        command = _video_segment_command(
            video_source="source.mp4",
            audio_source="enhanced.m4a",
            source_start=4.0,
            duration=2.0,
            vf="format=rgba,scale=64:64",
            scale_w=64,
            scale_h=64,
            crf=23,
            output=Path("out.mp4"),
            processed=False,
            audio_processed=True,
        )
        self.assertEqual(
            command[:8],
            ["ffmpeg", "-y", "-ss", "4.0", "-i", "source.mp4", "-i", "enhanced.m4a"],
        )
        self.assertIn("1:a?", command)
        self.assertIn("0:v", command)

    def test_an_additional_mask_multiplies_existing_cutout_alpha(self):
        graph = _masked_filter_complex("format=rgba,scale=64:64", 64, 64, matte_input=2)
        self.assertIn("alphaextract[source_alpha]", graph)
        self.assertIn("[2:v]format=gray[mask_alpha]", graph)
        self.assertIn("blend=all_mode=multiply[combined_alpha]", graph)

    def test_only_completed_owned_effect_urls_are_accepted(self):
        rows = [
            SimpleNamespace(
                result_data={
                    "effectType": "remove_bg",
                    "clipTarget": {"start": 2.0, "end": 5.0},
                    "outputUrl": "https://cdn.example/owned.webm",
                }
            )
        ]
        db = SimpleNamespace(query=lambda _model: _Query(rows))
        result = _approved_processed_ranges(
            db,
            7,
            [
                {"start": 2, "end": 5, "sourceUrl": "https://cdn.example/owned.webm"},
                {"start": 2, "end": 5, "sourceUrl": "file:///etc/passwd"},
            ],
        )
        self.assertEqual(result, {(2.0, 5.0): "https://cdn.example/owned.webm"})

    def test_enhanced_audio_is_resolved_by_owned_result_id_and_exact_timing(self):
        rows = [
            SimpleNamespace(
                id=81,
                result_data={
                    "effectType": "audio",
                    "clipTarget": {"start": 2.0, "end": 5.0},
                    "outputUrl": "https://cdn.example/enhanced.m4a",
                },
            )
        ]
        db = SimpleNamespace(query=lambda _model: _Query(rows))
        self.assertEqual(
            _approved_audio_ranges(
                db,
                7,
                [{"start": 2, "end": 5, "enhancedResultId": 81}],
            ),
            {(2.0, 5.0): {"source": "https://cdn.example/enhanced.m4a"}},
        )
        self.assertEqual(
            _approved_audio_ranges(
                db,
                7,
                [{"start": 2, "end": 6, "enhancedResultId": 81}],
            ),
            {},
        )

    def test_completed_retouch_visual_is_accepted_only_with_matching_effect_type(self):
        rows = [
            SimpleNamespace(
                result_data={
                    "effectType": "retouch",
                    "clipTarget": {"start": 2.0, "end": 5.0},
                    "outputUrl": "https://cdn.example/beauty.mp4",
                }
            )
        ]
        db = SimpleNamespace(query=lambda _model: _Query(rows))
        requested = [{
            "start": 2,
            "end": 5,
            "sourceUrl": "https://cdn.example/beauty.mp4",
            "effectType": "retouch",
        }]
        self.assertEqual(
            _approved_processed_ranges(db, 7, requested),
            {(2.0, 5.0): "https://cdn.example/beauty.mp4"},
        )
        requested[0]["effectType"] = "remove_bg"
        self.assertEqual(_approved_processed_ranges(db, 7, requested), {})

    def test_background_removal_never_consumes_a_stale_retouch(self):
        rows = [
            SimpleNamespace(
                id=8,
                status="completed",
                result_data={
                    "effectType": "retouch",
                    "clipKey": "video:range-1",
                    "outputUrl": "https://cdn.example/old.mp4",
                },
            ),
            SimpleNamespace(
                id=9,
                status="processing",
                result_data={
                    "effectType": "retouch",
                    "clipKey": "video:range-1",
                },
            ),
        ]
        db = SimpleNamespace(query=lambda _model: _Query(rows))
        self.assertIsNone(_completed_effect_source(db, 7, "video:range-1", "retouch"))

    def test_local_publish_keeps_the_webm_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "cutout.webm"
            source.write_bytes(b"webm")
            with (
                mock.patch(
                    "app.jobs.rough_cut_effect.cloudinary_credentials_configured",
                    return_value=False,
                ),
                mock.patch.dict("os.environ", {"UPLOADS_DIR": tmp}),
            ):
                url = _publish_output(source, 4, 9)
            self.assertTrue(url.endswith("/effect_9.webm"))
            self.assertTrue((Path(tmp) / "rough_cut_effects" / "4" / "effect_9.webm").exists())


if __name__ == "__main__":
    unittest.main()
