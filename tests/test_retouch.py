from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover - the whole class is skipped below
    cv2 = None
    np = None

from app.services.retouch.settings import (
    effective_retouch_settings,
    has_retouch_adjustments,
    sanitize_retouch_settings,
)


@unittest.skipIf(cv2 is None or np is None, "OpenCV retouch dependencies are not installed")
class RetouchTests(unittest.TestCase):
    def test_settings_are_clamped_and_legacy_auto_is_migrated(self):
        value = sanitize_retouch_settings({
            "autoStyles": True,
            "autoAmount": 999,
            "detailProtection": -4,
            "skinSmooth": float("nan"),
            "teethWhiten": 130,
            "targetFaces": "unknown",
        })
        self.assertTrue(value["autoRetouch"])
        self.assertEqual(value["autoAmount"], 100)
        self.assertEqual(value["detailProtection"], 0)
        self.assertEqual(value["skinSmooth"], 0)
        self.assertEqual(value["teethWhiten"], 100)
        self.assertEqual(value["targetFaces"], "all")

    def test_auto_retouch_adapts_brightness_to_face_luma(self):
        dark = effective_retouch_settings({"autoRetouch": True, "autoAmount": 70}, face_luma=70)
        bright = effective_retouch_settings({"autoRetouch": True, "autoAmount": 70}, face_luma=190)
        self.assertGreater(dark["skinBrighten"], bright["skinBrighten"])
        self.assertGreater(dark["glow"], bright["glow"])

    def test_retouch_activity_respects_enabled_auto_and_manual_settings(self):
        self.assertFalse(has_retouch_adjustments({"enabled": False, "skinSmooth": 80}))
        self.assertFalse(has_retouch_adjustments({"enabled": True, "skinSmooth": 0}))
        self.assertTrue(has_retouch_adjustments({"autoRetouch": True}))
        self.assertTrue(has_retouch_adjustments({"teethWhiten": 1}))

    def test_beauty_changes_only_the_detected_face_area(self):
        from app.services.retouch.beauty import BeautyState, beautify_frame

        class Detector:
            def detectMultiScale(self, *_args, **_kwargs):
                return [(30, 20, 70, 70)]

        frame = np.zeros((120, 140, 3), dtype=np.uint8)
        yy, xx = np.mgrid[0:70, 0:70]
        texture = ((xx + yy) % 17).astype(np.uint8)
        frame[20:90, 30:100] = np.stack((80 + texture, 125 + texture, 180 + texture), axis=-1)
        result = beautify_frame(frame, {"skinSmooth": 70, "evenTone": 50, "skinBrighten": 15}, BeautyState(detector=Detector()))
        changed = np.any(result != frame, axis=2)
        self.assertGreater(int(changed[20:90, 30:100].sum()), 100)
        outside = changed.copy()
        outside[20:90, 30:100] = False
        self.assertEqual(int(outside.sum()), 0)

    def test_profile_faces_are_found_when_frontal_detection_misses(self):
        from app.services.retouch.beauty import BeautyState

        class EmptyDetector:
            def detectMultiScale(self, *_args, **_kwargs):
                return []

        class ProfileDetector:
            def detectMultiScale(self, *_args, **_kwargs):
                return [(12, 18, 42, 50)]

        state = BeautyState(detector=EmptyDetector(), profile_detector=ProfileDetector())
        faces = state.locate(np.zeros((100, 160, 3), dtype=np.uint8), "all")
        self.assertEqual(len(faces), 2)
        self.assertEqual(faces[0][1:], (18, 42, 50))
        self.assertEqual(faces[1][1:], (18, 42, 50))
        self.assertLess(faces[0][0], faces[1][0])

    def test_detected_features_are_normalized_and_temporally_stabilized(self):
        from app.services.retouch.beauty import BeautyState

        class Detector:
            def __init__(self, boxes):
                self.boxes = boxes

            def detectMultiScale(self, *_args, **_kwargs):
                return self.boxes

        eye_detector = Detector([(15, 20, 20, 12), (65, 22, 20, 12)])
        smile_detector = Detector([(25, 12, 50, 20)])
        state = BeautyState(eye_detector=eye_detector, smile_detector=smile_detector)
        frame = np.zeros((130, 150, 3), dtype=np.uint8)
        face = (20, 10, 100, 100)

        first = state.features_for(frame, [face])[0]
        self.assertTrue(first.eyes_detected)
        self.assertTrue(first.mouth_detected)
        self.assertAlmostEqual(first.eyes[0][0], 0.25, places=2)
        self.assertAlmostEqual(first.eyes[1][0], 0.75, places=2)
        self.assertAlmostEqual(first.mouth[1], 0.68, places=2)

        eye_detector.boxes = [(19, 20, 20, 12), (69, 22, 20, 12)]
        second = state.features_for(frame, [face])[0]
        self.assertGreater(second.eyes[0][0], first.eyes[0][0])
        self.assertLess(second.eyes[0][0], 0.29)

    def test_yunet_landmarks_drive_face_features_when_available(self):
        from app.services.retouch.beauty import BeautyState

        class YuNetDetector:
            def setInputSize(self, _size):
                pass

            def detect(self, _frame):
                return 1, np.array([[20, 10, 100, 100, 45, 40, 95, 42, 70, 62, 50, 82, 90, 82, 0.99]])

        class EmptyDetector:
            def detectMultiScale(self, *_args, **_kwargs):
                return []

        state = BeautyState(
            yunet_detector=YuNetDetector(),
            detector=EmptyDetector(),
            profile_detector=EmptyDetector(),
        )
        frame = np.zeros((150, 160, 3), dtype=np.uint8)
        faces = state.locate(frame, "all")
        features = state.features_for(frame, faces)
        self.assertEqual(len(features), 1)
        self.assertTrue(features[0].eyes_detected)
        self.assertTrue(features[0].nose_detected)
        self.assertTrue(features[0].mouth_detected)
        self.assertLess(features[0].eyes[0][0], features[0].nose[0])
        self.assertLess(features[0].nose[0], features[0].eyes[1][0])
        self.assertGreater(features[0].mouth[1], features[0].nose[1])

    def test_analysis_exposes_normalized_targets_and_part_capabilities(self):
        from app.services.retouch.beauty import (
            FaceAnalysis,
            FaceFeatures,
            serialize_face_analysis,
        )

        frame = np.zeros((100, 200, 3), dtype=np.uint8)
        analysis = serialize_face_analysis(
            frame,
            [
                FaceAnalysis(
                    box=(20, 10, 80, 70),
                    features=FaceFeatures(
                        eyes_detected=True,
                        nose_detected=False,
                        mouth_detected=True,
                    ),
                )
            ],
        )
        self.assertEqual(analysis["width"], 200)
        self.assertAlmostEqual(analysis["detections"][0]["box"]["x"], 0.1)
        self.assertAlmostEqual(analysis["detections"][0]["box"]["height"], 0.7)
        self.assertTrue(analysis["capabilities"]["face"])
        self.assertTrue(analysis["capabilities"]["eyes"])
        self.assertTrue(analysis["capabilities"]["mouth"])
        self.assertFalse(analysis["capabilities"]["nose"])
        self.assertFalse(analysis["capabilities"]["multipleFaces"])
        self.assertIs(analysis["detections"][0]["parts"]["eyes"], True)
        self.assertIs(analysis["detections"][0]["parts"]["nose"], False)

        # Keep FastAPI's documented response contract locked to the detector's
        # actual payload; a mismatch here otherwise turns successful detection
        # into a response-validation 500.
        from app.api.routes.ai import VisualPreviewResponse

        response = VisualPreviewResponse(
            width=200,
            height=100,
            image_png="data:image/png;base64,test",
            face_count=1,
            retouch_analysis=analysis,
        )
        self.assertTrue(response.retouch_analysis.detections[0].parts.eyes)

    def test_skin_mask_adapts_to_strong_camera_color_cast(self):
        from app.services.retouch.beauty import FaceFeatures, _skin_mask

        # Blue-cast skin falls outside the fixed Cb threshold. The face-local
        # cheek/nose sample must still identify it instead of silently making
        # smoothing work only under neutral lighting.
        roi = np.full((100, 100, 3), (90, 45, 25), dtype=np.uint8)
        mask = _skin_mask(roi, FaceFeatures())
        self.assertGreater(float(mask[56, 50]), 0.65)

    def test_multi_face_tracking_claims_each_previous_face_once(self):
        from app.services.retouch.beauty import BeautyState

        class Detector:
            boxes = [(10, 10, 50, 50), (73, 10, 50, 50)]

            def detectMultiScale(self, *_args, **_kwargs):
                return self.boxes

        class EmptyDetector:
            def detectMultiScale(self, *_args, **_kwargs):
                return []

        detector = Detector()
        state = BeautyState(
            yunet_detector=None,
            detector=detector,
            profile_detector=EmptyDetector(),
        )
        frame = np.zeros((80, 140, 3), dtype=np.uint8)
        state.locate(frame, "all")
        state.locate(frame, "all")
        state.locate(frame, "all")
        detector.boxes = [(20, 10, 50, 50), (40, 25, 50, 50)]
        tracked = state.locate(frame, "all")
        self.assertEqual(len(tracked), 2)
        self.assertGreater(abs(tracked[1][0] - tracked[0][0]), 35)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg is required")
    def test_whole_clip_renderer_produces_a_playable_bounded_video(self):
        from app.services.retouch.video import render_retouch_video

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            output = root / "retouched.mp4"
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=160x96:r=8:d=0.5", "-pix_fmt", "yuv420p", str(source)],
                check=True,
                timeout=20,
            )
            progress: list[int] = []
            render_retouch_video(str(source), {"start": 0.125, "end": 0.375}, {"enabled": True, "skinSmooth": 30}, output, progress=progress.append)
            self.assertTrue(output.exists())
            duration = subprocess.check_output(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(output)],
                text=True,
                timeout=10,
            )
            self.assertGreater(float(duration.strip()), 0.1)
            self.assertLess(float(duration.strip()), 0.6)
            self.assertTrue(progress)


if __name__ == "__main__":
    unittest.main()
