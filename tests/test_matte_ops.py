"""Matte refinement: invert, grow/shrink, feather, strength.

These run on both sides of the WYSIWYG promise — the interactive preview calls
`refine_matte` on one frame and the export calls it on every frame — so a change
in behaviour here desynchronises what the user tunes from what they get.
"""

import unittest

import numpy as np

from app.services.segmentation.matte_ops import (
    MAX_EXPAND,
    MAX_FEATHER,
    is_identity,
    matte_settings_from_attributes,
    refine_matte,
)


def disc(size=200, radius=50):
    """A filled circle: has a curved edge, so morphology and blur both show."""
    yy, xx = np.mgrid[0:size, 0:size]
    inside = (yy - size // 2) ** 2 + (xx - size // 2) ** 2 <= radius**2
    return (inside.astype(np.uint8)) * 255


def kept(matte):
    return (matte > 127).mean()


class SettingsTests(unittest.TestCase):
    def test_absent_removebg_is_a_no_op(self):
        self.assertTrue(is_identity(matte_settings_from_attributes(None)))
        self.assertTrue(is_identity(matte_settings_from_attributes({})))

    def test_an_explicit_zero_is_not_replaced_by_a_default(self):
        # `or`-style defaulting here would silently re-apply a feather the user
        # had deliberately zeroed. That exact bug already bit the chroma key.
        settings = matte_settings_from_attributes(
            {"maskFeather": 0, "maskExpand": 0, "maskOpacity": 0}
        )
        self.assertEqual(settings["feather"], 0)
        self.assertEqual(settings["expand"], 0)
        self.assertEqual(settings["opacity"], 0)
        # opacity 0 is a real, non-identity request.
        self.assertFalse(is_identity(settings))

    def test_missing_opacity_defaults_to_fully_opaque(self):
        self.assertEqual(matte_settings_from_attributes({})["opacity"], 1.0)

    def test_values_are_clamped_to_sane_ranges(self):
        settings = matte_settings_from_attributes(
            {"maskExpand": 9, "maskFeather": 9, "maskOpacity": 9}
        )
        self.assertEqual(settings["expand"], MAX_EXPAND)
        self.assertEqual(settings["feather"], MAX_FEATHER)
        self.assertEqual(settings["opacity"], 1.0)

    def test_negative_expand_is_allowed_but_bounded(self):
        # Negative means shrink, which is a legitimate request.
        self.assertEqual(matte_settings_from_attributes({"maskExpand": -9})["expand"], -MAX_EXPAND)

    def test_garbage_falls_back_rather_than_raising(self):
        settings = matte_settings_from_attributes(
            {"maskFeather": "wide", "maskExpand": None, "maskOpacity": float("nan")}
        )
        self.assertTrue(is_identity(settings))

    def test_invert_is_read_from_invert_mask(self):
        self.assertTrue(matte_settings_from_attributes({"invertMask": True})["invert"])
        self.assertFalse(is_identity(matte_settings_from_attributes({"invertMask": True})))


class RefineTests(unittest.TestCase):
    def setUp(self):
        self.matte = disc()
        self.base = kept(self.matte)

    def test_identity_returns_the_matte_untouched(self):
        result = refine_matte(self.matte, matte_settings_from_attributes({}))
        np.testing.assert_array_equal(result, self.matte)

    def test_invert_swaps_kept_and_removed(self):
        result = refine_matte(self.matte, {"invert": True})
        np.testing.assert_array_equal(result, 255 - self.matte)
        self.assertAlmostEqual(kept(result), 1 - self.base, places=2)

    def test_invert_is_its_own_inverse(self):
        once = refine_matte(self.matte, {"invert": True})
        twice = refine_matte(once, {"invert": True})
        np.testing.assert_array_equal(twice, self.matte)

    def test_positive_expand_grows_the_kept_region(self):
        result = refine_matte(self.matte, {"expand": 0.05})
        self.assertGreater(kept(result), self.base)

    def test_negative_expand_shrinks_it(self):
        result = refine_matte(self.matte, {"expand": -0.05})
        self.assertLess(kept(result), self.base)

    def test_expand_refers_to_the_subject_even_when_inverted(self):
        # The ordering guarantee: grow means "grow what I selected" regardless of
        # invert. If invert were applied first, dilate would erode instead and the
        # control would feel backwards.
        grown_then_inverted = refine_matte(self.matte, {"expand": 0.05, "invert": True})
        plain_inverted = refine_matte(self.matte, {"invert": True})
        # Growing the subject must *shrink* the kept area of the inverted result.
        self.assertLess(kept(grown_then_inverted), kept(plain_inverted))

    def test_feather_creates_intermediate_values(self):
        hard = refine_matte(self.matte, {})
        soft = refine_matte(self.matte, {"feather": 0.05})
        self.assertEqual(np.unique(hard).tolist(), [0, 255])
        partial = ((soft > 0) & (soft < 255)).sum()
        self.assertGreater(partial, 0)

    def test_feather_keeps_the_interior_solid_and_exterior_empty(self):
        # A feather that bled into the middle of the subject would be a blur, not
        # an edge treatment.
        soft = refine_matte(self.matte, {"feather": 0.03})
        self.assertEqual(soft[100, 100], 255)
        self.assertEqual(soft[0, 0], 0)

    def test_opacity_scales_the_whole_matte(self):
        result = refine_matte(self.matte, {"opacity": 0.5})
        self.assertAlmostEqual(int(result[100, 100]), 127, delta=2)
        self.assertEqual(result[0, 0], 0)

    def test_opacity_zero_removes_everything(self):
        result = refine_matte(self.matte, {"opacity": 0.0})
        self.assertEqual(int(result.max()), 0)

    def test_output_is_always_uint8_of_the_same_shape(self):
        # ffmpeg's alphamerge will not resize or reinterpret for us.
        for settings in (
            {"invert": True},
            {"expand": 0.05},
            {"feather": 0.05},
            {"opacity": 0.3},
            {"invert": True, "expand": -0.02, "feather": 0.02, "opacity": 0.7},
        ):
            with self.subTest(settings=settings):
                result = refine_matte(self.matte, settings)
                self.assertEqual(result.dtype, np.uint8)
                self.assertEqual(result.shape, self.matte.shape)
                self.assertGreaterEqual(int(result.min()), 0)
                self.assertLessEqual(int(result.max()), 255)

    def test_radii_are_relative_to_the_shorter_edge(self):
        # The same setting must look the same at two resolutions, which is why
        # these are fractions and not pixels.
        small = disc(size=200, radius=50)
        large = disc(size=400, radius=100)
        grown_small = kept(refine_matte(small, {"expand": 0.05}))
        grown_large = kept(refine_matte(large, {"expand": 0.05}))
        self.assertAlmostEqual(grown_small, grown_large, places=2)

    def test_a_sub_pixel_radius_is_skipped_rather_than_rounded_up(self):
        # A tiny nonzero value must not become a full-pixel dilate, which would
        # make the control jump on the first increment.
        result = refine_matte(self.matte, {"expand": 1e-6})
        np.testing.assert_array_equal(result, self.matte)


if __name__ == "__main__":
    unittest.main()
