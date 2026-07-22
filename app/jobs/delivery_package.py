"""RQ job: build delivery zip package and upload artifact."""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import cloudinary.uploader
import httpx
from sqlalchemy.orm import Session

import app.utils.cloudinary  # noqa: F401
from app.db.database import SessionLocal
from app.db.models import DeliveryAsset, DeliveryExport, DeliveryPackage, Video, VideoTranscription

logger = logging.getLogger(__name__)


def delivery_package_job(package_id: int) -> None:
    db: Session = SessionLocal()
    try:
        pkg = db.query(DeliveryPackage).filter(DeliveryPackage.id == package_id).first()
        if not pkg:
            return
        video = db.query(Video).filter(Video.id == pkg.video_id).first()
        if not video or not video.file_path:
            _fail(db, pkg, "Video or file_path missing")
            return

        pkg.status = "processing"
        pkg.error_message = None
        db.add(pkg)
        db.commit()

        db.query(DeliveryAsset).filter(DeliveryAsset.delivery_package_id == pkg.id).delete()
        db.commit()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = root / "payload"
            payload.mkdir(parents=True, exist_ok=True)
            manifest: list[dict[str, str | int | None]] = []

            approved_path = _download_to_file(video.file_path, payload / "approved" / f"{video.name or 'approved'}.mp4")
            _append_manifest_and_asset(db, pkg, manifest, "approved_video", approved_path, video.file_path)

            source_path = _download_to_file(video.file_path, payload / "source" / f"{video.name or 'source'}.mp4")
            _append_manifest_and_asset(db, pkg, manifest, "source_file", source_path, video.file_path)

            if video.thumbnail_url:
                thumb_path = _download_to_file(video.thumbnail_url, payload / "thumbnails" / "thumbnail.jpg")
                _append_manifest_and_asset(db, pkg, manifest, "thumbnail", thumb_path, video.thumbnail_url)

            tx = db.query(VideoTranscription).filter(VideoTranscription.video_id == video.id).first()
            if tx and tx.segments:
                caption_path = payload / "captions" / "captions.json"
                caption_path.parent.mkdir(parents=True, exist_ok=True)
                caption_path.write_text(json.dumps(tx.segments, ensure_ascii=False, indent=2), encoding="utf-8")
                _append_manifest_and_asset(db, pkg, manifest, "caption", caption_path, "")

            exports = (
                db.query(DeliveryExport)
                .filter(DeliveryExport.video_id == video.id, DeliveryExport.status == "completed")
                .all()
            )
            for exp in exports:
                if not exp.output_path:
                    continue
                profile_name = exp.profile_key.replace("/", "_")
                out_path = _download_to_file(exp.output_path, payload / "exports" / f"{profile_name}.mp4")
                _append_manifest_and_asset(db, pkg, manifest, f"rendition_{profile_name}", out_path, exp.output_path)

            manifest_path = payload / "manifest.json"
            manifest_path.write_text(json.dumps({"assets": manifest}, ensure_ascii=False, indent=2), encoding="utf-8")

            zip_path = root / f"delivery-package-{pkg.id}.zip"
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for file in payload.rglob("*"):
                    if file.is_file():
                        zf.write(file, file.relative_to(payload))

            digest = _sha256_file(zip_path)
            from app.storage import build_key, get_storage

            _key = build_key(
                folder=os.environ.get("CLOUDINARY_DELIVERY_PACKAGE_FOLDER", "delivery_packages"),
                filename=zip_path.name,
                content_type="application/zip",
            )
            pkg.zip_url = get_storage().upload_path(
                zip_path, key=_key, content_type="application/zip"
            ).url
            pkg.zip_size_bytes = zip_path.stat().st_size
            pkg.checksum_sha256 = digest
            pkg.status = "completed"
            from datetime import datetime, timezone
            pkg.completed_at = datetime.now(timezone.utc)
            db.add(pkg)
            db.commit()
    except Exception as e:
        logger.exception("delivery_package_job failed for %s", package_id)
        pkg = db.query(DeliveryPackage).filter(DeliveryPackage.id == package_id).first()
        if pkg:
            _fail(db, pkg, str(e)[:4000])
    finally:
        db.close()


def _download_to_file(url: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if url.startswith("http://") or url.startswith("https://"):
        with httpx.Client(timeout=httpx.Timeout(600.0, connect=60.0)) as client:
            r = client.get(url, follow_redirects=True)
            r.raise_for_status()
            path.write_bytes(r.content)
        return path
    src = Path(url)
    path.write_bytes(src.read_bytes())
    return path


def _append_manifest_and_asset(
    db: Session,
    pkg: DeliveryPackage,
    manifest: list[dict[str, str | int | None]],
    asset_type: str,
    file_path: Path,
    file_url: str,
) -> None:
    digest = _sha256_file(file_path)
    mime, _ = mimetypes.guess_type(file_path.name)
    manifest.append(
        {
            "asset_type": asset_type,
            "filename": file_path.name,
            "size_bytes": file_path.stat().st_size,
            "checksum_sha256": digest,
        }
    )
    db.add(
        DeliveryAsset(
            delivery_package_id=pkg.id,
            asset_type=asset_type,
            file_url=file_url,
            filename=file_path.name,
            mime_type=mime,
            size_bytes=file_path.stat().st_size,
            checksum_sha256=digest,
        )
    )
    db.commit()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _fail(db: Session, pkg: DeliveryPackage, message: str) -> None:
    pkg.status = "failed"
    pkg.error_message = message[:4000]
    db.add(pkg)
    db.commit()
