from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

AssetKind = Literal["video_file", "thumbnail", "delivery_zip", "delivery_asset", "delivery_export"]


@dataclass
class ColdStorageResult:
    cold_uri: str
    size_bytes: int
    checksum_sha256: str
    provider: str


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_remote_to_path(url: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=httpx.Timeout(600.0, connect=30.0), follow_redirects=True) as client:
        with client.stream("GET", url) as res:
            res.raise_for_status()
            with destination.open("wb") as f:
                for chunk in res.iter_bytes(1024 * 1024):
                    f.write(chunk)
    return destination


def migrate_asset_to_cold_storage(
    *,
    project_id: int,
    source_url: str,
    kind: AssetKind,
    filename_hint: str,
) -> ColdStorageResult:
    """
    Move or copy an asset to the configured cold-storage backend.

    Supported providers:
    - local_fs: stores under COLD_STORAGE_LOCAL_DIR (default ./cold_storage)
    """
    provider = (os.getenv("COLD_STORAGE_PROVIDER", "local_fs") or "local_fs").strip().lower()
    if provider != "local_fs":
        raise RuntimeError(f"Unsupported cold storage provider: {provider}")

    base_dir = Path(os.getenv("COLD_STORAGE_LOCAL_DIR", "./cold_storage")).resolve()
    target = base_dir / f"project_{project_id}" / kind / filename_hint
    target.parent.mkdir(parents=True, exist_ok=True)

    src = (source_url or "").strip()
    if not src:
        raise ValueError("source_url is empty")

    if src.startswith("http://") or src.startswith("https://"):
        _copy_remote_to_path(src, target)
    else:
        src_path = Path(src)
        if not src_path.is_absolute():
            src_path = Path(os.getcwd()) / src_path
        if not src_path.exists():
            raise FileNotFoundError(f"Source file does not exist: {src_path}")
        if os.getenv("COLD_STORAGE_MOVE_LOCAL_FILES", "0").strip().lower() in {"1", "true", "yes"}:
            shutil.move(str(src_path), str(target))
        else:
            shutil.copy2(src_path, target)

    return ColdStorageResult(
        cold_uri=f"cold+local://{target}",
        size_bytes=target.stat().st_size,
        checksum_sha256=_sha256_file(target),
        provider="local_fs",
    )

