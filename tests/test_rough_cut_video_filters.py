import unittest
import subprocess

from app.jobs.rough_cut_export import (
    _blend_mode,
    _canvas_background_color,
    _clip_compositor_filter_complex,
    _motion_blur_filter_parts,
    _needs_clip_compositor,
)


class RoughCutVideoFilterTests(unittest.TestCase):
    def test_canvas_color_accepts_only_safe_hex(self):
        self.assertEqual(_canvas_background_color({"video": {"canvas": {"enabled": True, "color": "#1020aB"}}}), "0x1020aB")
        self.assertEqual(_canvas_background_color({"video": {"canvas": {"enabled": True, "color": "red;movie=/etc/passwd"}}}), "black")
        self.assertEqual(_canvas_background_color({"video": {"canvas": {"enabled": False, "color": "#ffffff"}}}), "black")

    def test_motion_blur_is_temporal_bounded_and_disabled_cleanly(self):
        self.assertEqual(_motion_blur_filter_parts({"video": {"motionBlur": {"enabled": False, "amount": 100}}}), [])
        parts = _motion_blur_filter_parts({"video": {"motionBlur": {"enabled": True, "amount": 100, "shutterAngle": 360}}})
        self.assertTrue(parts and parts[0].startswith("tmix=frames=8:"))
        self.assertEqual(_motion_blur_filter_parts({"video": {"motionBlur": {"enabled": True, "amount": 0}}}), [])

    def test_compositor_detection_is_scoped_to_real_visual_work(self):
        self.assertFalse(_needs_clip_compositor({"video": {"scale": 100, "opacity": 100}}))
        self.assertTrue(_needs_clip_compositor({"video": {"cornerRadius": 8}}))
        self.assertTrue(_needs_clip_compositor({"animation": {"inPreset": "fade"}}))
        self.assertTrue(_needs_clip_compositor({"keyframes": {"video.x": [{"t": 0, "v": 0}]}}))
        self.assertTrue(_needs_clip_compositor({"video": {"blendMode": "screen"}}))
        self.assertEqual(_blend_mode({"video": {"blendMode": "soft-light"}}), "softlight")
        self.assertEqual(_blend_mode({"video": {"blendMode": "movie=/etc/passwd"}}), "normal")

    def test_ffmpeg_accepts_every_exposed_blend_mode(self):
        for blend_mode in ("multiply", "screen", "overlay", "soft-light", "difference", "color-dodge"):
            with self.subTest(blend_mode=blend_mode):
                graph = _clip_compositor_filter_complex(
                    vf="scale=320:180,format=rgba",
                    scale_w=320,
                    scale_h=180,
                    duration=0.2,
                    frame_rate=30,
                    settings={"video": {"blendMode": blend_mode, "scale": 72, "cornerRadius": 8}},
                    processed=False,
                    matte_input=None,
                )
                result = subprocess.run(
                    [
                        "ffmpeg", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=0.2",
                        "-filter_complex", graph, "-map", "[v]", "-frames:v", "6", "-f", "null", "-",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_ffmpeg_accepts_keyframed_transform_gradient_and_rounding(self):
        graph = _clip_compositor_filter_complex(
            vf="scale=320:180,format=rgba",
            scale_w=320,
            scale_h=180,
            duration=0.4,
            frame_rate=30,
            settings={
                "video": {
                    "scale": 110,
                    "cornerRadius": 12,
                    "canvas": {"enabled": True, "mode": "gradient", "color": "#102030", "colorEnd": "#405060", "angle": 135},
                    "motionBlur": {"enabled": True, "amount": 25, "shutterAngle": 180},
                },
                "animation": {"inPreset": "slide-left", "duration": 0.2},
                "keyframes": {"video.x": [{"t": 0, "v": -20}, {"t": 0.4, "v": 10, "easing": "ease-in-out"}]},
            },
            processed=False,
            matte_input=None,
        )
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=0.4",
                "-filter_complex", graph, "-map", "[v]", "-frames:v", "12", "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ffmpeg_accepts_a_synced_blurred_canvas(self):
        graph = _clip_compositor_filter_complex(
            vf="scale=320:180,format=rgba",
            scale_w=320,
            scale_h=180,
            duration=0.2,
            frame_rate=30,
            settings={"video": {"canvas": {"enabled": True, "mode": "blur", "blur": 18, "dim": 20}}},
            processed=False,
            matte_input=None,
        )
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=0.2",
                "-filter_complex", graph, "-map", "[v]", "-frames:v", "6", "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_processed_cutout_blur_and_matte_keep_their_input_indices(self):
        graph = _clip_compositor_filter_complex(
            vf="scale=320:180,format=rgba",
            scale_w=320,
            scale_h=180,
            duration=0.2,
            frame_rate=30,
            settings={"video": {"rotation": 5, "canvas": {"enabled": True, "mode": "blur", "blur": 12}}},
            processed=True,
            matte_input=2,
        )
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=red@0.8:size=320x180:rate=30:duration=0.2,format=rgba",
                "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=0.2",
                "-f", "lavfi", "-i", "color=white:size=320x180:rate=30:duration=0.2,format=gray",
                "-filter_complex", graph, "-map", "[v]", "-frames:v", "6", "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
