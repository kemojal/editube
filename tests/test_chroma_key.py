"""Backend half of the chroma key cross-language contract.

Reads the same fixture the frontend test reads. If the two sides ever disagree,
the preview and the render disagree, and a user sees one thing and exports
another — which is the only failure mode that really matters here.
"""

import json
import unittest
from pathlib import Path

from app.services.chroma_key import (
    build_chroma_key_filter,
    chroma_key_from_attributes,
    parse_hex_color,
)

FIXTURE = Path(__file__).parent / "fixtures" / "chroma_key_cases.json"


class ChromaKeyContractTests(unittest.TestCase):
    def test_matches_shared_golden(self):
        cases = json.loads(FIXTURE.read_text())["cases"]
        self.assertGreater(len(cases), 10, "fixture looks truncated")

        for case in cases:
            with self.subTest(case=case["name"]):
                settings = chroma_key_from_attributes(case["removeBg"])
                self.assertEqual(build_chroma_key_filter(settings), case["filter"])


class ParseHexColorTests(unittest.TestCase):
    def test_accepts_shorthand_and_full(self):
        self.assertEqual(parse_hex_color("#0f0"), (0, 255, 0))
        self.assertEqual(parse_hex_color("00FF00"), (0, 255, 0))

    def test_rejects_malformed(self):
        for bad in ["", "#", "#12", "#12345", "#gggggg", "rgb(0,255,0)", None, 5]:
            self.assertIsNone(parse_hex_color(bad))


class AttributeReadTests(unittest.TestCase):
    def test_zero_is_not_treated_as_missing(self):
        # `.get(k) or default` would silently turn a hard-edge key soft.
        settings = chroma_key_from_attributes({"chromaKey": True, "blend": 0, "similarity": 0})
        self.assertEqual(settings.blend, 0.0)
        self.assertEqual(settings.similarity, 0.0)

    def test_missing_fields_use_defaults(self):
        settings = chroma_key_from_attributes({"chromaKey": True})
        self.assertAlmostEqual(settings.similarity, 0.4)
        self.assertAlmostEqual(settings.blend, 0.1)

    def test_none_attributes_are_disabled(self):
        self.assertFalse(chroma_key_from_attributes(None).enabled)


if __name__ == "__main__":
    unittest.main()
