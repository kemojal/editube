"""Archive/cold-storage transition job for old projects."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.db.models import (
    ActivityFeed,
    DeliveryAsset,
    DeliveryExport,
    DeliveryPackage,
    ProjectArchiveState,
    ProjectRetentionPolicy,
    Video,
)
from app.services.cold_storage import migrate_asset_to_cold_storage

logger = logging.getLogger(__name__)


def archive_cold_storage_job(project_id: int) -> None:
    db: Session = SessionLocal()
    try:
        policy = db.query(ProjectRetentionPolicy).filter(ProjectRetentionPolicy.project_id == project_id).first()
        if not policy:
            policy = ProjectRetentionPolicy(project_id=project_id, auto_archive_enabled=True, archive_after_days=90)
            db.add(policy)
            db.commit()
            db.refresh(policy)
        if not policy.auto_archive_enabled:
            return

        state = db.query(ProjectArchiveState).filter(ProjectArchiveState.project_id == project_id).first()
        now = datetime.now(timezone.utc)
        if not state:
            state = ProjectArchiveState(project_id=project_id, state="archived", archived_at=now)
            db.add(state)
        elif state.state == "active":
            state.state = "archived"
            state.archived_at = now
        else:
            migrated = _migrate_project_assets_to_cold(db, project_id)
            state.state = "cold_storage"
            state.cold_moved_at = now
            state.storage_location_meta = {
                "provider": policy.cold_tier_provider or "local_fs",
                "moved_at": now.isoformat(),
                "assets": migrated,
            }
        policy.last_archive_run_at = now
        db.add(policy)
        db.add(state)
        db.add(
            ActivityFeed(
                project_id=project_id,
                user_id=None,
                action="project_archive_state_changed",
                meta_info=f"state={state.state}",
            )
        )
        db.commit()
    except Exception:
        logger.exception("archive_cold_storage_job failed for project %s", project_id)
    finally:
        db.close()


def _migrate_project_assets_to_cold(db: Session, project_id: int) -> list[dict]:
    migrated: list[dict] = []
    videos = db.query(Video).filter(Video.project_id == project_id).all()
    for video in videos:
        migrated.extend(
            _migrate_video_asset(
                project_id=project_id,
                source_url=video.file_path,
                kind="video_file",
                filename_hint=f"video_{video.id}.bin",
                row_id=video.id,
                table="videos",
                field="file_path",
            )
        )
        if video.thumbnail_url:
            migrated.extend(
                _migrate_video_asset(
                    project_id=project_id,
                    source_url=video.thumbnail_url,
                    kind="thumbnail",
                    filename_hint=f"video_{video.id}_thumb.bin",
                    row_id=video.id,
                    table="videos",
                    field="thumbnail_url",
                )
            )

    exports = (
        db.query(DeliveryExport)
        .join(Video, Video.id == DeliveryExport.video_id)
        .filter(Video.project_id == project_id, DeliveryExport.output_path.isnot(None))
        .all()
    )
    for exp in exports:
        migrated.extend(
            _migrate_video_asset(
                project_id=project_id,
                source_url=exp.output_path,
                kind="delivery_export",
                filename_hint=f"delivery_export_{exp.id}.bin",
                row_id=exp.id,
                table="delivery_exports",
                field="output_path",
            )
        )

    packages = db.query(DeliveryPackage).filter(DeliveryPackage.project_id == project_id).all()
    for pkg in packages:
        if pkg.zip_url:
            migrated.extend(
                _migrate_video_asset(
                    project_id=project_id,
                    source_url=pkg.zip_url,
                    kind="delivery_zip",
                    filename_hint=f"delivery_package_{pkg.id}.zip",
                    row_id=pkg.id,
                    table="delivery_packages",
                    field="zip_url",
                )
            )
        assets = db.query(DeliveryAsset).filter(DeliveryAsset.delivery_package_id == pkg.id).all()
        for asset in assets:
            migrated.extend(
                _migrate_video_asset(
                    project_id=project_id,
                    source_url=asset.file_url,
                    kind="delivery_asset",
                    filename_hint=f"delivery_asset_{asset.id}.bin",
                    row_id=asset.id,
                    table="delivery_assets",
                    field="file_url",
                )
            )
    return migrated


def _migrate_video_asset(
    *,
    project_id: int,
    source_url: str | None,
    kind: str,
    filename_hint: str,
    row_id: int,
    table: str,
    field: str,
) -> list[dict]:
    src = (source_url or "").strip()
    if not src:
        return []
    try:
        result = migrate_asset_to_cold_storage(
            project_id=project_id,
            source_url=src,
            kind=kind,  # type: ignore[arg-type]
            filename_hint=filename_hint,
        )
        return [
            {
                "table": table,
                "row_id": row_id,
                "field": field,
                "source_url": src,
                "cold_uri": result.cold_uri,
                "size_bytes": result.size_bytes,
                "checksum_sha256": result.checksum_sha256,
                "provider": result.provider,
            }
        ]
    except Exception as exc:
        logger.warning("Cold storage migrate failed for %s:%s id=%s: %s", table, field, row_id, exc)
        return [
            {
                "table": table,
                "row_id": row_id,
                "field": field,
                "source_url": src,
                "error": str(exc),
            }
        ]
