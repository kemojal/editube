import unittest

from app.services.mfa_totp import (
    generate_recovery_codes,
    generate_totp_secret,
    hash_recovery_codes,
    verify_recovery_code,
    verify_totp_code,
)


class MFATotpFlowTests(unittest.TestCase):
    def test_totp_secret_and_invalid_code(self) -> None:
        secret = generate_totp_secret()
        self.assertGreaterEqual(len(secret), 16)
        self.assertFalse(verify_totp_code(secret, "000000"))

    def test_recovery_code_hash_and_verify(self) -> None:
        raw_codes = generate_recovery_codes(3)
        hashes = hash_recovery_codes(raw_codes)
        matched = verify_recovery_code(raw_codes[0], hashes)
        self.assertIsNotNone(matched)
        self.assertIsNone(verify_recovery_code("dead-beef", hashes))


if __name__ == "__main__":
    unittest.main()
