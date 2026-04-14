import unittest
from types import SimpleNamespace

from app.services.youtube_chapters import (
    chapter_lines_from_rows,
    format_chapter_timestamp,
    merge_description_with_chapters,
    youtube_description_block,
)


class YoutubeChaptersTests(unittest.TestCase):
    def test_format_chapter_timestamp(self) -> None:
        self.assertEqual(format_chapter_timestamp(0), "0:00")
        self.assertEqual(format_chapter_timestamp(65), "1:05")
        self.assertEqual(format_chapter_timestamp(3661), "1:01:01")

    def test_chapter_lines_from_rows_orders_and_formats(self) -> None:
        rows = [
            SimpleNamespace(start_time=120, title="Mid", order_index=0),
            SimpleNamespace(start_time=0, title="Intro", order_index=0),
        ]
        self.assertEqual(
            chapter_lines_from_rows(rows),
            ["0:00 Intro", "2:00 Mid"],
        )

    def test_merge_description_with_chapters(self) -> None:
        lines = ["0:00 Start", "1:00 Part two"]
        merged = merge_description_with_chapters("Hello world", lines)
        self.assertIn("Hello world", merged)
        self.assertIn("0:00 Start", merged)
        self.assertIn("--- Chapters ---", merged)

    def test_youtube_description_block_joins_only_lines(self) -> None:
        rows = [SimpleNamespace(start_time=0, title="A", order_index=0)]
        self.assertEqual(youtube_description_block(rows), "0:00 A")


if __name__ == "__main__":
    unittest.main()
