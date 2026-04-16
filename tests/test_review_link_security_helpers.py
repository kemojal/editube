import unittest
from types import SimpleNamespace

from fastapi import HTTPException

from app.api.routes.review_links import _nda_identity_key, _enforce_geofence_or_403


class ReviewLinkSecurityHelperTests(unittest.TestCase):
    def test_nda_identity_prefers_email(self) -> None:
        a = _nda_identity_key(fingerprint="fp-1", guest_email="client@example.com")
        b = _nda_identity_key(fingerprint="fp-2", guest_email="client@example.com")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 64)

    def test_nda_identity_fallbacks_to_fingerprint(self) -> None:
        key = _nda_identity_key(fingerprint="fp-only", guest_email=None)
        self.assertEqual(len(key), 64)

    def test_geofence_allowlist_denies_unlisted_country(self) -> None:
        link = SimpleNamespace(
            geofence_mode="allowlist",
            geo_allow_countries=["US", "CA"],
            geo_block_countries=[],
        )
        with self.assertRaises(HTTPException):
            _enforce_geofence_or_403(link, "JP")

    def test_geofence_blocklist_allows_non_blocked_country(self) -> None:
        link = SimpleNamespace(
            geofence_mode="blocklist",
            geo_allow_countries=[],
            geo_block_countries=["RU", "CN"],
        )
        _enforce_geofence_or_403(link, "US")


if __name__ == "__main__":
    unittest.main()
