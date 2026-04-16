from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.services.cold_storage import migrate_asset_to_cold_storage


class ColdStorageTests(unittest.TestCase):
    def test_migrate_local_file_to_cold_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src_dir = Path(tmp) / "src"
            cold_dir = Path(tmp) / "cold"
            src_dir.mkdir(parents=True, exist_ok=True)
            source = src_dir / "sample.txt"
            source.write_text("hello cold storage", encoding="utf-8")

            os.environ["COLD_STORAGE_PROVIDER"] = "local_fs"
            os.environ["COLD_STORAGE_LOCAL_DIR"] = str(cold_dir)
            os.environ["COLD_STORAGE_MOVE_LOCAL_FILES"] = "0"

            result = migrate_asset_to_cold_storage(
                project_id=101,
                source_url=str(source),
                kind="video_file",
                filename_hint="sample.txt",
            )
            self.assertEqual(result.provider, "local_fs")
            self.assertTrue(result.cold_uri.startswith("cold+local://"))
            self.assertGreater(result.size_bytes, 0)
            self.assertEqual(len(result.checksum_sha256), 64)
            self.assertTrue(source.exists())  # copy mode should keep source


if __name__ == "__main__":
    unittest.main()

