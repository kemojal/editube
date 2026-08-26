"""WHISPER_FALLBACK_SIZE: deployment-wide floor for Whisper stand-in runs."""

import os
import unittest
from unittest import mock

from app.services.transcription_models import resolve_runtime


class FallbackSizeOverrideTests(unittest.TestCase):
    def test_default_stand_in_size_without_env(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WHISPER_FALLBACK_SIZE", None)
            model, size, is_fallback = resolve_runtime("parakeet-v3")
        self.assertEqual(model.id, "parakeet-v3")
        self.assertTrue(is_fallback)
        self.assertEqual(size, "small")

    def test_env_raises_stand_in_size(self):
        with mock.patch.dict(os.environ, {"WHISPER_FALLBACK_SIZE": "medium"}):
            model, size, is_fallback = resolve_runtime("parakeet-v3")
        self.assertTrue(is_fallback)
        self.assertEqual(size, "medium")

    def test_explicit_whisper_choice_is_never_overridden(self):
        with mock.patch.dict(os.environ, {"WHISPER_FALLBACK_SIZE": "large-v3"}):
            model, size, is_fallback = resolve_runtime("whisper-tiny")
        self.assertFalse(is_fallback)
        self.assertEqual(size, "tiny")

    def test_english_only_override_ignored_for_multilingual_model(self):
        # parakeet-v3 is multilingual; a *.en stand-in would drop languages.
        with mock.patch.dict(os.environ, {"WHISPER_FALLBACK_SIZE": "medium.en"}):
            _model, size, _ = resolve_runtime("parakeet-v3")
        self.assertEqual(size, "small")

    def test_english_only_override_allowed_for_english_model(self):
        with mock.patch.dict(os.environ, {"WHISPER_FALLBACK_SIZE": "medium.en"}):
            _model, size, _ = resolve_runtime("parakeet-v2")
        self.assertEqual(size, "medium.en")

    def test_invalid_size_is_ignored(self):
        with mock.patch.dict(os.environ, {"WHISPER_FALLBACK_SIZE": "enormous-v9"}):
            _model, size, _ = resolve_runtime("parakeet-v3")
        self.assertEqual(size, "small")


if __name__ == "__main__":
    unittest.main()
