"""Prompt extraction from the editor's selection.

Testable without torch, deliberately: this is the translation between what the
user drew and what SAM receives, and it is where an off-by-one or a dropped
label would silently produce the wrong subject.
"""

import unittest

from app.services.segmentation.local import LocalSegmentationProvider

extract = LocalSegmentationProvider._selection_prompts


class SelectionPromptTests(unittest.TestCase):
    def test_no_selection_returns_none(self):
        # None rather than empty lists, so the caller can switch between
        # prompted and automatic matting on a single condition.
        self.assertIsNone(extract({}))
        self.assertIsNone(extract({"selection": {}}))
        self.assertIsNone(extract({"selection": {"points": [], "strokes": []}}))

    def test_include_and_exclude_become_labels_one_and_zero(self):
        prompts = extract(
            {
                "selection": {
                    "points": [
                        {"x": 0.5, "y": 0.5, "include": True},
                        {"x": 0.1, "y": 0.2, "include": False},
                    ]
                }
            }
        )
        self.assertIsNotNone(prompts)
        points, labels = prompts.flattened()
        self.assertEqual(points, [(0.5, 0.5), (0.1, 0.2)])
        self.assertEqual(labels, [1, 0])

    def test_missing_include_defaults_to_positive(self):
        prompts = extract({"selection": {"points": [{"x": 0.5, "y": 0.5}]}})
        self.assertIsNotNone(prompts)
        _, labels = prompts.flattened()
        self.assertEqual(labels, [1])

    def test_each_include_click_is_an_independent_additive_group(self):
        prompts = extract(
            {
                "selection": {
                    "points": [
                        {"x": 0.2, "y": 0.5, "include": True},
                        {"x": 0.8, "y": 0.5, "include": True},
                    ]
                }
            }
        )
        self.assertIsNotNone(prompts)
        self.assertEqual(prompts.positive_groups, (((0.2, 0.5),), ((0.8, 0.5),)))

    def test_malformed_points_are_skipped_not_crashed_on(self):
        result = extract(
            {
                "selection": {
                    "points": [
                        {"x": 0.5, "y": 0.5, "include": True},
                        {"x": 0.5},
                        "nonsense",
                        None,
                    ]
                }
            }
        )
        self.assertIsNotNone(result)
        points, labels = result.flattened()
        self.assertEqual(len(points), 1)
        self.assertEqual(len(labels), 1)

    def test_strokes_become_sampled_point_runs(self):
        stroke = {"points": [{"x": i / 100, "y": 0.5} for i in range(100)], "include": True}
        prompts = extract({"selection": {"points": [], "strokes": [stroke]}})
        self.assertIsNotNone(prompts)
        points, labels = prompts.flattened()

        # Sampled, not sent wholesale: SAM degrades with a very large prompt set,
        # and 100 near-identical points add nothing over a dozen.
        self.assertGreater(len(points), 1)
        self.assertLessEqual(len(points), 14)
        self.assertTrue(all(label == 1 for label in labels))

    def test_eraser_strokes_carry_the_negative_label(self):
        stroke = {"points": [{"x": 0.1, "y": 0.1}, {"x": 0.2, "y": 0.2}], "include": False}
        prompts = extract(
            {
                "selection": {
                    "points": [{"x": 0.5, "y": 0.5, "include": True}],
                    "strokes": [stroke],
                }
            }
        )
        self.assertIsNotNone(prompts)
        _, labels = prompts.flattened()
        self.assertEqual(labels[0], 1)
        self.assertTrue(all(label == 0 for label in labels[1:]))

    def test_negative_marks_without_a_subject_return_none(self):
        self.assertIsNone(
            extract(
                {
                    "selection": {
                        "points": [{"x": 0.5, "y": 0.5, "include": False}],
                        "strokes": [
                            {
                                "points": [{"x": 0.1, "y": 0.1}],
                                "include": False,
                            }
                        ],
                    }
                }
            )
        )

    def test_points_and_strokes_combine(self):
        prompts = extract(
            {
                "selection": {
                    "points": [{"x": 0.5, "y": 0.5, "include": True}],
                    "strokes": [
                        {"points": [{"x": 0.1, "y": 0.1}, {"x": 0.2, "y": 0.2}], "include": False}
                    ],
                }
            }
        )
        self.assertIsNotNone(prompts)
        points, labels = prompts.flattened()
        self.assertGreater(len(points), 1)
        self.assertIn(0, labels)
        self.assertIn(1, labels)

    def test_labels_and_points_always_align(self):
        # A length mismatch here is the kind of bug that segments the wrong
        # thing rather than raising.
        result = extract(
            {
                "selection": {
                    "points": [{"x": 0.3, "y": 0.3, "include": False}],
                    "strokes": [
                        {"points": [{"x": 0.1, "y": 0.1}, {"x": 0.9, "y": 0.9}], "include": True}
                    ],
                }
            }
        )
        self.assertIsNotNone(result)
        points, labels = result.flattened()
        self.assertEqual(len(points), len(labels))

    def test_short_stroke_is_not_dropped_by_sampling(self):
        stroke = {"points": [{"x": 0.1, "y": 0.1}, {"x": 0.2, "y": 0.2}], "include": True}
        prompts = extract({"selection": {"strokes": [stroke]}})
        self.assertIsNotNone(prompts)
        points, _ = prompts.flattened()
        self.assertGreaterEqual(len(points), 1)

    def test_coordinates_are_clamped_to_the_source_frame(self):
        prompts = extract(
            {
                "selection": {
                    "points": [
                        {"x": -2, "y": 3, "include": True},
                        {"x": 4, "y": -1, "include": False},
                    ]
                }
            }
        )
        self.assertIsNotNone(prompts)
        self.assertEqual(prompts.positive_groups, (((0.0, 1.0),),))
        self.assertEqual(prompts.negative_points, ((1.0, 0.0),))


if __name__ == "__main__":
    unittest.main()
