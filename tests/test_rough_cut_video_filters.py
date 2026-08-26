import unittest
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

from app.jobs.rough_cut_export import (
    _blend_mode,
    _canvas_background_color,
    _clip_compositor_filter_complex,
    _crop_alpha_expression,
    _crop_is_active,
    _dynamic_zoom_settings,
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


class RoughCutCropTests(unittest.TestCase):
    """Cropping, against the contract in `_lib/viewer/clip-crop.ts`."""

    def _render(self, settings: dict, path: Path, *, source: str = "color=white:s=320x180:r=30:d=0.2"):
        graph = _clip_compositor_filter_complex(
            vf="scale=320:180,format=rgba",
            scale_w=320,
            scale_h=180,
            duration=0.2,
            frame_rate=30,
            settings=settings,
            processed=False,
            matte_input=None,
        )
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", source,
                "-filter_complex", graph, "-map", "[v]", "-frames:v", "1", "-y", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return Image.open(path).convert("RGB")

    def test_crop_is_only_active_once_switched_on(self):
        self.assertFalse(_needs_clip_compositor({"video": {"crop": {"left": 25}}}))
        self.assertFalse(_needs_clip_compositor({"video": {"crop": {"enabled": True}}}))
        self.assertTrue(_needs_clip_compositor({"video": {"crop": {"enabled": True, "left": 25}}}))
        self.assertIsNone(_crop_alpha_expression({"video": {"crop": {"enabled": False, "left": 25}}}, 1.0))
        self.assertIsNotNone(_crop_alpha_expression({"video": {"crop": {"enabled": True, "top": 5}}}, 1.0))

    def test_an_animated_crop_draws_unless_it_was_switched_off(self):
        """Parity with `sampleClipAttributes`, which enables an animated crop."""
        track = {"video.cropLeft": [{"t": 0, "v": 0}, {"t": 1, "v": 30}]}
        self.assertTrue(_crop_is_active({"keyframes": track}, 1.0))
        self.assertFalse(_crop_is_active({"video": {"crop": {"enabled": False}}, "keyframes": track}, 1.0))
        self.assertFalse(_crop_is_active({"video": {"crop": {"left": 25}}}, 1.0))

    def test_crop_cuts_the_frame_where_the_editor_said_it_would(self):
        with tempfile.TemporaryDirectory() as tmp:
            frame = self._render(
                {"video": {"crop": {"enabled": True, "left": 25, "bottom": 50}}},
                Path(tmp) / "crop.png",
            )
            # 25% of 320 is x=80; 50% of 180 is y=90.
            self.assertEqual(frame.getpixel((70, 40))[0], 0)
            self.assertEqual(frame.getpixel((90, 40))[0], 255)
            self.assertEqual(frame.getpixel((160, 80))[0], 255)
            self.assertEqual(frame.getpixel((160, 120))[0], 0)

    def test_softness_feathers_the_cut_edge(self):
        with tempfile.TemporaryDirectory() as tmp:
            frame = self._render(
                {"video": {"crop": {"enabled": True, "left": 25, "softness": 10}}},
                Path(tmp) / "soft.png",
            )
            # Softness is a share of the shorter edge: 10% of 180 is an 18px ramp.
            ramp = [frame.getpixel((x, 90))[0] for x in (78, 86, 94, 102)]
            self.assertEqual(ramp[0], 0)
            self.assertEqual(ramp[-1], 255)
            self.assertEqual(ramp, sorted(ramp))
            self.assertTrue(0 < ramp[1] < 255, ramp)

    def test_softness_leaves_uncropped_edges_hard(self):
        """Feathering an edge the crop never touched would be a vignette."""
        with tempfile.TemporaryDirectory() as tmp:
            frame = self._render(
                {"video": {"crop": {"enabled": True, "left": 25, "softness": 10}}},
                Path(tmp) / "soft_edges.png",
            )
            self.assertEqual(frame.getpixel((319, 90))[0], 255)
            self.assertEqual(frame.getpixel((200, 0))[0], 255)
            self.assertEqual(frame.getpixel((200, 179))[0], 255)

    def test_retain_position_decides_what_a_scale_grows_from(self):
        """Off, scale works about the cropped box; on, about the whole frame."""
        with tempfile.TemporaryDirectory() as tmp:
            spans = {}
            for retain in (False, True):
                frame = self._render(
                    {"video": {"scale": 200, "crop": {"enabled": True, "left": 50, "retainPosition": retain}}},
                    Path(tmp) / f"retain_{retain}.png",
                )
                row = [frame.getpixel((x, 90))[0] for x in range(320)]
                spans[retain] = next(x for x, value in enumerate(row) if value > 128)
            # Kept half starts at x=160 unscaled. Retaining the position scales
            # about the frame centre, which leaves that edge where it was; the
            # default scales about the cropped centre (x=240) and drags it to 80.
            self.assertAlmostEqual(spans[True], 160, delta=4)
            self.assertAlmostEqual(spans[False], 80, delta=4)

    def test_ffmpeg_accepts_a_keyframed_crop_over_a_rotated_scaled_clip(self):
        graph = _clip_compositor_filter_complex(
            vf="scale=320:180,format=rgba",
            scale_w=320,
            scale_h=180,
            duration=0.4,
            frame_rate=30,
            settings={
                "video": {
                    "scale": 130,
                    "rotation": 12,
                    "crop": {"enabled": True, "left": 10, "right": 10, "top": 5, "bottom": 5, "softness": 6},
                },
                "keyframes": {
                    "video.cropLeft": [{"t": 0, "v": 0}, {"t": 0.4, "v": 30, "easing": "ease-in-out"}],
                    "video.cropSoftness": [{"t": 0, "v": 0}, {"t": 0.4, "v": 20, "easing": "linear"}],
                },
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
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_over_cropped_axis_leaves_a_strip_rather_than_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            frame = self._render(
                {"video": {"crop": {"enabled": True, "left": 80, "right": 40}}},
                Path(tmp) / "over.png",
            )
            row = [frame.getpixel((x, 90))[0] for x in range(320)]
            self.assertTrue(any(value > 128 for value in row), "the whole clip was cropped away")


class RoughCutDynamicZoomTests(unittest.TestCase):
    """Dynamic Zoom, against the contract in `_lib/viewer/dynamic-zoom.ts`."""

    def _white_width(self, path: Path) -> int:
        frame = Image.open(path).convert("RGB")
        return sum(1 for x in range(320) if frame.getpixel((x, 90))[0] > 128)

    def _render_move(self, settings: dict, directory: Path, frames: int = 10):
        graph = _clip_compositor_filter_complex(
            vf="scale=320:180,format=rgba",
            scale_w=320,
            scale_h=180,
            duration=1.0,
            frame_rate=10,
            settings=settings,
            processed=False,
            matte_input=None,
        )
        # A half-width white card on transparency: its on-screen width is the
        # zoom, measured rather than inferred from the expression.
        source = "color=white:s=160x90:r=10:d=1,pad=320:180:80:45:color=black@0,format=rgba"
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", source,
                "-filter_complex", graph, "-map", "[v]", "-frames:v", str(frames),
                "-y", str(directory / "dz_%02d.png"),
            ],
            capture_output=True,
            text=True,
            timeout=40,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return [self._white_width(directory / f"dz_{index:02d}.png") for index in range(1, frames + 1)]

    def test_a_move_to_the_same_framing_is_not_a_move(self):
        self.assertIsNone(_dynamic_zoom_settings({"video": {"dynamicZoom": {"enabled": False}}}))
        self.assertIsNone(
            _dynamic_zoom_settings(
                {"video": {"dynamicZoom": {"enabled": True, "start": {"scale": 140}, "end": {"scale": 140}}}}
            )
        )
        self.assertIsNotNone(_dynamic_zoom_settings({"video": {"dynamicZoom": {"enabled": True}}}))
        self.assertFalse(
            _needs_clip_compositor(
                {"video": {"dynamicZoom": {"enabled": True, "start": {"scale": 100}, "end": {"scale": 100}}}}
            )
        )
        self.assertTrue(_needs_clip_compositor({"video": {"dynamicZoom": {"enabled": True}}}))

    def test_an_unknown_ease_falls_back_to_linear(self):
        zoom = _dynamic_zoom_settings({"video": {"dynamicZoom": {"enabled": True, "ease": "movie=/etc/passwd"}}})
        self.assertEqual(zoom["ease"], "linear")

    def test_the_move_fills_the_clip_and_grows_all_the_way(self):
        with tempfile.TemporaryDirectory() as tmp:
            widths = self._render_move(
                {"video": {"dynamicZoom": {"enabled": True, "ease": "linear",
                                           "start": {"scale": 100}, "end": {"scale": 200}}}},
                Path(tmp),
            )
            self.assertEqual(widths, sorted(widths))
            # Card is 160px at 100%; the last frame sits at 190% of the move.
            self.assertAlmostEqual(widths[0], 160, delta=6)
            self.assertAlmostEqual(widths[-1], 304, delta=8)

    def test_swapping_the_boxes_reverses_the_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            widths = self._render_move(
                {"video": {"dynamicZoom": {"enabled": True, "ease": "linear",
                                           "start": {"scale": 200}, "end": {"scale": 100}}}},
                Path(tmp),
            )
            self.assertEqual(widths, sorted(widths, reverse=True))
            self.assertAlmostEqual(widths[0], 320, delta=8)

    def test_a_pan_moves_the_picture_across_the_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph = _clip_compositor_filter_complex(
                vf="scale=320:180,format=rgba",
                scale_w=320,
                scale_h=180,
                duration=1.0,
                frame_rate=10,
                settings={"video": {"dynamicZoom": {"enabled": True, "ease": "linear",
                                                    "start": {"scale": 100, "x": -20},
                                                    "end": {"scale": 100, "x": 20}}}},
                processed=False,
                matte_input=None,
            )
            source = "color=white:s=80x90:r=10:d=1,pad=320:180:120:45:color=black@0,format=rgba"
            result = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", source,
                    "-filter_complex", graph, "-map", "[v]", "-frames:v", "10",
                    "-y", str(Path(tmp) / "pan_%02d.png"),
                ],
                capture_output=True,
                text=True,
                timeout=40,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            centres = []
            for index in (1, 10):
                frame = Image.open(Path(tmp) / f"pan_{index:02d}.png").convert("RGB")
                lit = [x for x in range(320) if frame.getpixel((x, 90))[0] > 128]
                centres.append(sum(lit) / len(lit))
            # -20% to +20% of a 320px frame is 128px of travel, less the ~13px
            # the last frame of ten has yet to cover.
            self.assertAlmostEqual(centres[1] - centres[0], 115, delta=10)

    def test_ffmpeg_accepts_a_dynamic_zoom_stacked_on_everything_else(self):
        graph = _clip_compositor_filter_complex(
            vf="scale=320:180,format=rgba",
            scale_w=320,
            scale_h=180,
            duration=0.4,
            frame_rate=30,
            settings={
                "video": {
                    "scale": 120,
                    "rotation": 8,
                    "cornerRadius": 10,
                    "crop": {"enabled": True, "left": 8, "top": 6, "softness": 4},
                    "dynamicZoom": {"enabled": True, "ease": "ease-in-out",
                                    "start": {"scale": 100, "x": -10, "y": 4},
                                    "end": {"scale": 150, "x": 12, "y": -8}},
                    "canvas": {"enabled": True, "mode": "gradient", "color": "#102030", "colorEnd": "#405060"},
                },
                "animation": {"inPreset": "slide-left", "duration": 0.2},
                "keyframes": {"video.x": [{"t": 0, "v": -10}, {"t": 0.4, "v": 8, "easing": "ease-in-out"}]},
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
            timeout=40,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
