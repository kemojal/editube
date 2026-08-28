from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from app.api.models.delivery import (
    DeliveryLinkCreate,
    DeliveryLinkResponse,
    DeliveryPackageCreate,
    DeliveryPackageDetailResponse,
    DeliveryPackageResponse,
    DeliveryPublicInfo,
    DeliveryRenewRequest,
)
from app.db.database import get_db
from app.db.models import (
    ActivityFeed,
    DeliveryAsset,
    DeliveryLink,
    DeliveryPackage,
    DeliveryReceipt,
    Project,
    User,
    Video,
)
from app.jobs.queue import enqueue_delivery_package_job
from app.services.project_access import can_access_project
from app.services.workspace_branding_resolve import branding_public_dict
from app.services.product_analytics import emit_once
from app.utils.security import get_current_user

router = APIRouter(prefix="/delivery", tags=["Delivery"])
public_router = APIRouter(prefix="/delivery", tags=["Delivery (public)"])

_DEFAULT_EXPIRES_DAYS = int(os.getenv("DELIVERY_LINK_DEFAULT_DAYS", "30"))


def _emit_delivery_event(
    db: Session,
    event_name: str,
    *,
    project: Project,
    event_id: str,
    user: User | None = None,
    user_id: int | None = None,
    properties: dict | None = None,
) -> None:
    emit_once(
        db,
        event_name,
        event_id=event_id,
        user=user,
        user_id=user_id,
        workspace_id=project.workspace_id,
        properties={"project_id": project.id, **(properties or {})},
    )


def _emit_delivery_download(
    db: Session,
    *,
    project: Project,
    scope_id: str,
    properties: dict,
    user: User | None = None,
) -> None:
    download_id = secrets.token_hex(12)
    common = {"feature_key": "delivery", "result": "success", **properties}
    _emit_delivery_event(
        db,
        "delivery_downloaded",
        project=project,
        event_id=f"delivery:{scope_id}:download:{download_id}",
        user=user,
        properties=common,
    )
    _emit_delivery_event(
        db,
        "feature_result_used",
        project=project,
        event_id=f"feature:delivery:{scope_id}:download:{download_id}",
        user=user,
        properties={**common, "result_type": "download"},
    )


def _require_delivery_enabled() -> None:
    if os.getenv("DELIVERY_PACKAGES_ENABLED", "1").strip().lower() in {"0", "false", "off"}:
        raise HTTPException(status_code=404, detail="Delivery packages are disabled")


def _require_owner_project(project_id: int, db: Session, current_user: User) -> Project:
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_access_project(db, current_user.id, project):
        raise HTTPException(status_code=403, detail="Not authorized")
    return project


def _get_delivery_link_or_404(token: str, db: Session) -> DeliveryLink:
    link = db.query(DeliveryLink).filter(DeliveryLink.token == token).first()
    if not link:
        raise HTTPException(status_code=404, detail="Delivery link not found")
    return link


def _is_expired(link: DeliveryLink) -> bool:
    now = datetime.now(timezone.utc)
    exp = link.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp < now


@router.post("/packages", response_model=DeliveryPackageResponse)
def create_delivery_package(
    body: DeliveryPackageCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_delivery_enabled()
    video = db.query(Video).filter(Video.id == body.video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    project = _require_owner_project(video.project_id, db, current_user)
    pkg = DeliveryPackage(
        project_id=video.project_id,
        video_id=video.id,
        approved_version_id=body.approved_version_id or body.video_id,
        status="queued",
        requested_by_user_id=current_user.id,
    )
    db.add(pkg)
    db.flush()
    _emit_delivery_event(
        db,
        "delivery_package_started",
        project=project,
        event_id=f"delivery-package:{pkg.id}:started",
        user=current_user,
        properties={"delivery_package_id": pkg.id, "video_id": video.id, "result": "success"},
    )
    _emit_delivery_event(
        db,
        "feature_started",
        project=project,
        event_id=f"feature:delivery:package:{pkg.id}:started",
        user=current_user,
        properties={"feature_key": "delivery", "delivery_package_id": pkg.id, "result": "success"},
    )
    db.commit()
    db.refresh(pkg)
    if not enqueue_delivery_package_job(pkg.id):
        pkg.status = "failed"
        pkg.error_message = "Could not queue package build (set REDIS_URL and run an RQ worker)."
        db.add(pkg)
        _emit_delivery_event(
            db,
            "feature_failed",
            project=project,
            event_id=f"feature:delivery:package:{pkg.id}:queue-failed",
            user=current_user,
            properties={
                "feature_key": "delivery",
                "delivery_package_id": pkg.id,
                "failure_class": "queue",
                "error_code": "delivery_queue_unavailable",
                "result": "failure",
            },
        )
        db.commit()
        db.refresh(pkg)
    return pkg


@router.get("/packages/{package_id}", response_model=DeliveryPackageDetailResponse)
def get_delivery_package(
    package_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_delivery_enabled()
    pkg = db.query(DeliveryPackage).filter(DeliveryPackage.id == package_id).first()
    if not pkg:
        raise HTTPException(status_code=404, detail="Delivery package not found")
    project = _require_owner_project(pkg.project_id, db, current_user)
    return pkg


@router.get("/projects/{project_id}/packages", response_model=list[DeliveryPackageResponse])
def list_project_delivery_packages(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_delivery_enabled()
    _require_owner_project(project_id, db, current_user)
    return (
        db.query(DeliveryPackage)
        .filter(DeliveryPackage.project_id == project_id)
        .order_by(DeliveryPackage.created_at.desc())
        .all()
    )


@router.get("/projects/{project_id}/videos")
def list_project_videos(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_delivery_enabled()
    _require_owner_project(project_id, db, current_user)
    rows = (
        db.query(Video)
        .filter(Video.project_id == project_id)
        .order_by(Video.created_at.desc())
        .all()
    )
    return {
        "ok": True,
        "items": [
            {
                "id": v.id,
                "name": v.name,
                "status": v.status,
                "version": v.version,
                "created_at": v.created_at,
            }
            for v in rows
        ],
    }


@router.get("/packages/{package_id}/download-url")
def package_download_url(
    package_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_delivery_enabled()
    pkg = db.query(DeliveryPackage).filter(DeliveryPackage.id == package_id).first()
    if not pkg:
        raise HTTPException(status_code=404, detail="Delivery package not found")
    _require_owner_project(pkg.project_id, db, current_user)
    if not pkg.zip_url:
        raise HTTPException(status_code=400, detail="Package zip is not ready")
    _emit_delivery_download(
        db,
        project=project,
        scope_id=f"package:{pkg.id}:owner",
        user=current_user,
        properties={
            "delivery_package_id": pkg.id,
            "download_scope": "package",
            "actor_type": "owner",
        },
    )
    db.commit()
    return {"ok": True, "url": pkg.zip_url}


@router.post("/packages/{package_id}/links", response_model=DeliveryLinkResponse)
def create_delivery_link(
    package_id: int,
    body: DeliveryLinkCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_delivery_enabled()
    pkg = db.query(DeliveryPackage).filter(DeliveryPackage.id == package_id).first()
    if not pkg:
        raise HTTPException(status_code=404, detail="Delivery package not found")
    _require_owner_project(pkg.project_id, db, current_user)
    days = max(1, min(365, int(body.expires_in_days or _DEFAULT_EXPIRES_DAYS)))
    link = DeliveryLink(
        delivery_package_id=pkg.id,
        token=secrets.token_urlsafe(24),
        expires_at=datetime.now(timezone.utc) + timedelta(days=days),
        created_by_user_id=current_user.id,
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    return link


@router.get("/packages/{package_id}/links", response_model=list[DeliveryLinkResponse])
def list_delivery_links(
    package_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_delivery_enabled()
    pkg = db.query(DeliveryPackage).filter(DeliveryPackage.id == package_id).first()
    if not pkg:
        raise HTTPException(status_code=404, detail="Delivery package not found")
    _require_owner_project(pkg.project_id, db, current_user)
    return (
        db.query(DeliveryLink)
        .filter(DeliveryLink.delivery_package_id == package_id)
        .order_by(DeliveryLink.created_at.desc())
        .all()
    )


@router.get("/packages/{package_id}/receipts")
def list_delivery_receipts(
    package_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_delivery_enabled()
    pkg = db.query(DeliveryPackage).filter(DeliveryPackage.id == package_id).first()
    if not pkg:
        raise HTTPException(status_code=404, detail="Delivery package not found")
    _require_owner_project(pkg.project_id, db, current_user)
    rows = (
        db.query(DeliveryReceipt, DeliveryAsset)
        .join(DeliveryLink, DeliveryLink.id == DeliveryReceipt.delivery_link_id)
        .outerjoin(DeliveryAsset, DeliveryAsset.id == DeliveryReceipt.delivery_asset_id)
        .filter(DeliveryLink.delivery_package_id == pkg.id)
        .order_by(DeliveryReceipt.downloaded_at.desc())
        .all()
    )
    totals = (
        db.query(
            func.count(DeliveryReceipt.id),
            func.count(func.distinct(DeliveryReceipt.guest_email)),
            func.max(DeliveryReceipt.downloaded_at),
        )
        .join(DeliveryLink, DeliveryLink.id == DeliveryReceipt.delivery_link_id)
        .filter(DeliveryLink.delivery_package_id == pkg.id)
        .first()
    )
    return {
        "ok": True,
        "summary": {
            "total_downloads": int(totals[0] or 0),
            "unique_downloaders": int(totals[1] or 0),
            "last_downloaded_at": totals[2],
        },
        "items": [
            {
                "id": receipt.id,
                "asset_id": receipt.delivery_asset_id,
                "asset_name": asset.filename if asset else "package.zip",
                "guest_name": receipt.guest_name,
                "guest_email": receipt.guest_email,
                "ip_address": receipt.ip_address,
                "user_agent": receipt.user_agent,
                "downloaded_at": receipt.downloaded_at,
            }
            for receipt, asset in rows
        ],
    }


@public_router.get("/{token}", response_model=DeliveryPublicInfo)
def public_delivery_info(token: str, db: Session = Depends(get_db)):
    _require_delivery_enabled()
    link = _get_delivery_link_or_404(token, db)
    if link.is_revoked:
        raise HTTPException(status_code=410, detail="Delivery link revoked")
    expired = _is_expired(link)
    pkg = db.query(DeliveryPackage).filter(DeliveryPackage.id == link.delivery_package_id).first()
    if not pkg:
        raise HTTPException(status_code=404, detail="Delivery package not found")
    project = db.query(Project).filter(Project.id == pkg.project_id).first()
    branding = branding_public_dict(db, project) if project else None
    return DeliveryPublicInfo(
        token=link.token,
        package=pkg,
        workspace_branding=branding,
        expires_at=link.expires_at,
        expired=expired,
    )


@public_router.get("/{token}/assets/{asset_id}/download-url")
def public_delivery_asset_download_url(
    token: str,
    asset_id: int,
    request: Request,
    session_id: Optional[str] = None,
    guest_name: Optional[str] = None,
    guest_email: Optional[str] = None,
    db: Session = Depends(get_db),
):
    _require_delivery_enabled()
    link = _get_delivery_link_or_404(token, db)
    if link.is_revoked or _is_expired(link):
        raise HTTPException(status_code=410, detail="Delivery link not available")
    asset = (
        db.query(DeliveryAsset)
        .filter(DeliveryAsset.id == asset_id)
        .first()
    )
    if not asset or asset.delivery_package_id != link.delivery_package_id:
        raise HTTPException(status_code=404, detail="Asset not found")
    pkg = db.query(DeliveryPackage).filter(DeliveryPackage.id == link.delivery_package_id).first()
    project = db.query(Project).filter(Project.id == pkg.project_id).first() if pkg else None
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    receipt = DeliveryReceipt(
        delivery_link_id=link.id,
        delivery_asset_id=asset.id,
        session_id=session_id,
        guest_name=guest_name,
        guest_email=guest_email,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    db.add(receipt)
    db.add(
        ActivityFeed(
            project_id=project.id,
            user_id=None,
            action="delivery_asset_downloaded",
            meta_info=f"{asset.asset_type}:{asset.filename}",
        )
    )
    _emit_delivery_download(
        db,
        project=project,
        scope_id=f"asset:{asset.id}:guest",
        properties={
            "delivery_package_id": pkg.id,
            "delivery_asset_id": asset.id,
            "asset_type": asset.asset_type,
            "download_scope": "asset",
            "actor_type": "guest",
        },
    )
    db.commit()
    return {"ok": True, "url": asset.file_url}


@public_router.get("/{token}/package/download")
def public_delivery_package_download(
    token: str,
    request: Request,
    session_id: Optional[str] = None,
    guest_name: Optional[str] = None,
    guest_email: Optional[str] = None,
    db: Session = Depends(get_db),
):
    _require_delivery_enabled()
    link = _get_delivery_link_or_404(token, db)
    if link.is_revoked or _is_expired(link):
        raise HTTPException(status_code=410, detail="Delivery link not available")
    pkg = db.query(DeliveryPackage).filter(DeliveryPackage.id == link.delivery_package_id).first()
    if not pkg or not pkg.zip_url:
        raise HTTPException(status_code=404, detail="Package zip not ready")
    project = db.query(Project).filter(Project.id == pkg.project_id).first()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    db.add(
        DeliveryReceipt(
            delivery_link_id=link.id,
            delivery_asset_id=None,
            session_id=session_id,
            guest_name=guest_name,
            guest_email=guest_email,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    )
    _emit_delivery_download(
        db,
        project=project,
        scope_id=f"package:{pkg.id}:guest",
        properties={
            "delivery_package_id": pkg.id,
            "download_scope": "package",
            "actor_type": "guest",
        },
    )
    db.commit()
    return RedirectResponse(url=pkg.zip_url, status_code=302)


@router.post("/{token}/renew", response_model=DeliveryLinkResponse)
def renew_delivery_link(
    token: str,
    body: DeliveryRenewRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_delivery_enabled()
    link = _get_delivery_link_or_404(token, db)
    pkg = db.query(DeliveryPackage).filter(DeliveryPackage.id == link.delivery_package_id).first()
    if not pkg:
        raise HTTPException(status_code=404, detail="Delivery package not found")
    _require_owner_project(pkg.project_id, db, current_user)
    extend_days = max(1, min(365, int(body.extend_days)))
    base = link.expires_at
    now = datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    if base < now:
        base = now
    link.expires_at = base + timedelta(days=extend_days)
    link.renew_count = int(link.renew_count or 0) + 1
    link.last_renewed_at = now
    db.add(link)
    db.commit()
    db.refresh(link)
    return link
