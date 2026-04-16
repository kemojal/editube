import unittest
from types import SimpleNamespace

from app.services.forensic_watermark import build_forensic_fingerprint
from app.services.oidc_sso import validate_oidc_claims


class ForensicAndOIDCClaimTests(unittest.TestCase):
    def test_forensic_fingerprint_shape(self) -> None:
        link = SimpleNamespace(id=11)
        session = SimpleNamespace(id=22, guest_email="reviewer@example.com", ip_address="1.1.1.1")
        fp = build_forensic_fingerprint(link, session, "US")
        self.assertEqual(len(fp), 64)

    def test_oidc_claim_validation_accepts_matching_claims(self) -> None:
        provider = SimpleNamespace(issuer="https://issuer.example.com", client_id="abc123")
        claims = validate_oidc_claims(
            provider=provider,
            id_token=None,
            userinfo={
                "iss": "https://issuer.example.com",
                "aud": "abc123",
                "email": "user@example.com",
                "email_verified": True,
            },
        )
        self.assertEqual(claims["email"], "user@example.com")

    def test_oidc_claim_validation_rejects_wrong_audience(self) -> None:
        provider = SimpleNamespace(issuer="https://issuer.example.com", client_id="abc123")
        with self.assertRaises(ValueError):
            validate_oidc_claims(
                provider=provider,
                id_token=None,
                userinfo={
                    "iss": "https://issuer.example.com",
                    "aud": "wrong-client",
                    "email": "user@example.com",
                },
            )


if __name__ == "__main__":
    unittest.main()
