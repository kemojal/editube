import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.services.comment_export import (
    export_comments_csv,
    export_comments_edl,
    export_comments_fcpxml,
    export_comments_pdf,
    export_comments_premiere_xml,
)


def _mock_comment(cid, tc, text, parent_id=None, kind="comment", status="open"):
    m = MagicMock()
    m.id = cid
    m.parent_id = parent_id
    m.timecode = tc
    m.end_timecode = None
    m.text = text
    m.kind = kind
    m.status = status
    m.user_id = None
    m.user = None
    m.guest_name = "Guest"
    m.guest_email = None
    m.created_at = datetime.now(timezone.utc)
    return m


class CommentExportTests(unittest.TestCase):
    def test_csv_contains_header_and_row(self) -> None:
        rows = [_mock_comment(1, 12, "Fix color")]
        data = export_comments_csv(rows).decode("utf-8")
        self.assertIn("timecode_sec", data)
        self.assertIn("Fix color", data)
        self.assertIn("12", data)

    def test_edl_starts_with_title(self) -> None:
        rows = [_mock_comment(1, 0, "Note")]
        data = export_comments_edl(rows).decode("utf-8")
        self.assertTrue(data.startswith("TITLE:"))

    def test_fcpxml_is_xml(self) -> None:
        rows = [_mock_comment(1, 5, "Marker")]
        data = export_comments_fcpxml(rows)
        self.assertTrue(data.startswith(b"<?xml"))
        self.assertIn(b"fcpxml", data)

    def test_premiere_xml_is_xmeml(self) -> None:
        rows = [_mock_comment(1, 3, "Cut here")]
        data = export_comments_premiere_xml(rows)
        self.assertIn(b"xmeml", data)

    def test_pdf_magic(self) -> None:
        rows = [_mock_comment(1, 1, "PDF row")]
        pdf = export_comments_pdf(rows, "Test video")
        self.assertTrue(pdf.startswith(b"%PDF"))
