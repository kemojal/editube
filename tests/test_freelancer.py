import unittest
from datetime import datetime, timezone

from app.services.contract_pdf import build_signed_contract_pdf
from app.utils.return_path import safe_internal_path


class SafeInternalPathTests(unittest.TestCase):
    def test_accepts_simple_path(self) -> None:
        self.assertEqual(safe_internal_path("/projects/5/business"), "/projects/5/business")

    def test_rejects_protocol_relative(self) -> None:
        self.assertEqual(safe_internal_path("//evil.com"), "/projects")

    def test_empty_uses_default(self) -> None:
        self.assertEqual(safe_internal_path(""), "/projects")


class ContractPdfTests(unittest.TestCase):
    def test_outputs_pdf_magic(self) -> None:
        signed = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        pdf = build_signed_contract_pdf(
            title="Test agreement",
            body="Line one\nLine two",
            signer_name="Ada L",
            signer_email="ada@example.com",
            signature_data="Ada L",
            signed_at=signed,
        )
        self.assertTrue(pdf.startswith(b"%PDF"))

    def test_png_data_url_signature_produces_pdf(self) -> None:
        signed = datetime.now(timezone.utc)
        png_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        sig = f"data:image/png;base64,{png_b64}"
        pdf = build_signed_contract_pdf("T", "B", "N", "e@e.com", sig, signed)
        self.assertTrue(pdf.startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
