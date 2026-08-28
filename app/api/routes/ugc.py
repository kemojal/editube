"""AI UGC Ads API.

Paste a product/app URL → extract → brief + hooks/scripts → campaign → generate
N creator-style ad variations → ad library. Workspace-scoped; gated behind
``FEATURE_AI_UGC`` (on unless explicitly disabled). Provider work runs via RQ;
when ``REDIS_URL`` is unset the relevant job runs inline so the flow still works
in local dev (stub + ``UGC_RENDER_DRY_RUN``).
"""

from __future__ import annotations

import logging
import os
import secrets
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.api.models.ugc import (
    CampaignCreate,
    GenerateRequest,
    PerformanceCreate,
    ProductCreate,
    RegenerateRequest,
)
from app.db.database import get_db
from app.db.models import (
    UgcAvatar,
    UgcBrief,
    UgcCampaign,
    UgcPerformance,
    UgcProduct,
    UgcVariation,
    UgcVoice,
    User,
    Workspace,
)
from app.jobs.queue import (
    enqueue_ugc_brief_generate_job,
    enqueue_ugc_product_import_job,
    enqueue_ugc_render_job,
)
from app.services import ugc_credits
from app.services.pricing import get_plan_spec
from app.services.project_access import get_workspace_member
from app.services.ugc_catalog import sync_provider_catalog
from app.services.ugc_compliance import platform_guidance
from app.services.ugc_learner import analyze_campaign
from app.services.ugc_platforms import get_preset, list_presets
from app.services.ugc_variation_engine import InsufficientCreditsError, build_variations
from app.ugc_providers import get_avatar_provider
from app.utils.security import get_current_user
from app.services.product_analytics import emit, emit_once

logger = logging.getLogger(__name__)


def _feature_enabled() -> bool:
    return os.getenv("FEATURE_AI_UGC", "1").strip().lower() not in ("0", "false", "no", "off")


def _require_feature() -> None:
    if not _feature_enabled():
        raise HTTPException(status_code=404, detail="AI UGC is not enabled")


router = APIRouter(prefix="/ugc", tags=["AI UGC"], dependencies=[Depends(_require_feature)])
public_router = APIRouter(prefix="/ugc", tags=["AI UGC"])  # provider webhooks (no auth)


# --- workspace / access helpers -------------------------------------------


def _resolve_workspace(db: Session, user: User, workspace_id: Optional[int]) -> Workspace:
    if workspace_id is not None:
        ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
        if not ws:
            raise HTTPException(status_code=404, detail="Workspace not found")
        if ws.owner_user_id != user.id and not get_workspace_member(db, ws.id, user.id):
            raise HTTPException(status_code=403, detail="Not a member of this workspace")
        return ws
    ws = db.query(Workspace).filter(Workspace.owner_user_id == user.id).order_by(Workspace.id.asc()).first()
    if ws:
        return ws
    membership = next(iter(user.workspace_memberships or []), None)
    if membership:
        ws = db.query(Workspace).filter(Workspace.id == membership.workspace_id).first()
        if ws:
            return ws
    raise HTTPException(status_code=400, detail="No workspace available; create a workspace first")


def _assert_ws_access(db: Session, user: User, workspace_id: int) -> None:
    ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    if ws and ws.owner_user_id == user.id:
        return
    wm = get_workspace_member(db, workspace_id, user.id)
    if not wm or wm.role == "client":
        raise HTTPException(status_code=403, detail="Not authorized for this workspace")


def _product_or_404(db: Session, user: User, product_id: int) -> UgcProduct:
    p = db.query(UgcProduct).filter(UgcProduct.id == product_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Product not found")
    _assert_ws_access(db, user, p.workspace_id)
    return p


def _campaign_or_404(db: Session, user: User, campaign_id: int) -> UgcCampaign:
    c = db.query(UgcCampaign).filter(UgcCampaign.id == campaign_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Campaign not found")
    _assert_ws_access(db, user, c.workspace_id)
    return c


def _variation_or_404(db: Session, user: User, variation_id: int) -> tuple[UgcVariation, UgcCampaign]:
    v = db.query(UgcVariation).filter(UgcVariation.id == variation_id).first()
    if not v:
        raise HTTPException(status_code=404, detail="Variation not found")
    c = db.query(UgcCampaign).filter(UgcCampaign.id == v.campaign_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Campaign not found")
    _assert_ws_access(db, user, c.workspace_id)
    return v, c


# --- serialization ---------------------------------------------------------


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


def _product_out(p: UgcProduct) -> dict[str, Any]:
    return {
        "id": p.id,
        "workspace_id": p.workspace_id,
        "source_url": p.source_url,
        "source_type": p.source_type,
        "name": p.name,
        "brand": p.brand,
        "price": p.price,
        "currency": p.currency,
        "description": p.description,
        "benefits": list(p.benefits or []),
        "pain_points": list(p.pain_points or []),
        "use_cases": list(p.use_cases or []),
        "target_audience": p.target_audience,
        "reviews": list(p.reviews or []),
        "image_urls": list(p.image_urls or []),
        "status": p.status,
        "error_message": p.error_message,
        "created_at": _iso(p.created_at),
    }


def _brief_out(b: UgcBrief) -> dict[str, Any]:
    return {
        "id": b.id,
        "product_id": b.product_id,
        "audience": b.audience,
        "main_promise": b.main_promise,
        "pain_points": list(b.pain_points or []),
        "objections": list(b.objections or []),
        "benefits": list(b.benefits or []),
        "angles": list(b.angles or []),
        "hooks": list(b.hooks or []),
        "scripts": list(b.scripts or []),
        "ctas": list(b.ctas or []),
        "status": b.status,
        "error_message": b.error_message,
        "created_at": _iso(b.created_at),
    }


def _campaign_out(c: UgcCampaign, db: Session | None = None) -> dict[str, Any]:
    out = {
        "id": c.id,
        "workspace_id": c.workspace_id,
        "product_id": c.product_id,
        "brief_id": c.brief_id,
        "name": c.name,
        "platform": c.platform,
        "default_aspect_ratio": c.default_aspect_ratio,
        "default_length_sec": c.default_length_sec,
        "status": c.status,
        "created_at": _iso(c.created_at),
        "disclosure_guidance": platform_guidance(c.platform),
    }
    if db is not None:
        out["variation_count"] = (
            db.query(UgcVariation).filter(UgcVariation.campaign_id == c.id).count()
        )
    return out


def _variation_out(v: UgcVariation) -> dict[str, Any]:
    return {
        "id": v.id,
        "campaign_id": v.campaign_id,
        "name": v.name,
        "angle": v.angle,
        "hook": v.hook,
        "script": v.script,
        "cta": v.cta,
        "caption_style": v.caption_style,
        "provider": v.provider,
        "provider_avatar_id": v.provider_avatar_id,
        "provider_voice_id": v.provider_voice_id,
        "avatar_name": v.avatar_name,
        "voice_name": v.voice_name,
        "aspect_ratio": v.aspect_ratio,
        "length_sec": v.length_sec,
        "status": v.status,
        "render_progress": v.render_progress,
        "render_error": v.render_error,
        "storage_url": v.storage_url,
        "thumbnail_url": v.thumbnail_url,
        "is_ai_generated": v.is_ai_generated,
        "disclosure_applied": v.disclosure_applied,
        "completed_at": _iso(v.completed_at),
        "created_at": _iso(v.created_at),
    }


def _no_redis() -> bool:
    return not os.environ.get("REDIS_URL", "").strip()


def _inline_render_if_no_redis(created: list[int]) -> None:
    """Dev/demo: with no worker, render synchronously (capped) so ads appear."""
    if _no_redis() and created:
        from app.jobs.ugc_render import ugc_render_job

        for vid in created[:25]:
            ugc_render_job(vid)


# --- products --------------------------------------------------------------


@router.post("/products")
def create_product(
    body: ProductCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict[str, Any]:
    ws = _resolve_workspace(db, user, body.workspace_id)
    url = (body.url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="A valid http(s) product URL is required")
    product = UgcProduct(user_id=user.id, workspace_id=ws.id, source_url=url, status="pending")
    db.add(product)
    db.commit()
    db.refresh(product)

    job_id = enqueue_ugc_product_import_job(product.id)
    if not job_id and _no_redis():
        from app.jobs.ugc_product_import import ugc_product_import_job

        ugc_product_import_job(product.id)
        db.refresh(product)
    return _product_out(product)


@router.get("/products")
def list_products(
    workspace_id: Optional[int] = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    ws = _resolve_workspace(db, user, workspace_id)
    rows = (
        db.query(UgcProduct)
        .filter(UgcProduct.workspace_id == ws.id)
        .order_by(UgcProduct.id.desc())
        .limit(100)
        .all()
    )
    return {"products": [_product_out(p) for p in rows], "workspace_id": ws.id}


@router.get("/products/{product_id}")
def get_product(
    product_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict[str, Any]:
    p = _product_or_404(db, user, product_id)
    briefs = db.query(UgcBrief).filter(UgcBrief.product_id == p.id).order_by(UgcBrief.id.desc()).all()
    out = _product_out(p)
    out["briefs"] = [_brief_out(b) for b in briefs]
    return out


@router.post("/products/{product_id}/brief")
def generate_brief_endpoint(
    product_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict[str, Any]:
    p = _product_or_404(db, user, product_id)
    brief = UgcBrief(product_id=p.id, status="pending")
    db.add(brief)
    db.commit()
    db.refresh(brief)

    job_id = enqueue_ugc_brief_generate_job(p.id)
    if not job_id and _no_redis():
        from app.jobs.ugc_brief_generate import ugc_brief_generate_job

        ugc_brief_generate_job(p.id)
        db.refresh(brief)
    return _brief_out(brief)


@router.get("/briefs/{brief_id}")
def get_brief(
    brief_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict[str, Any]:
    b = db.query(UgcBrief).filter(UgcBrief.id == brief_id).first()
    if not b:
        raise HTTPException(status_code=404, detail="Brief not found")
    p = db.query(UgcProduct).filter(UgcProduct.id == b.product_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Product not found")
    _assert_ws_access(db, user, p.workspace_id)
    return _brief_out(b)


# --- campaigns -------------------------------------------------------------


@router.post("/campaigns")
def create_campaign(
    body: CampaignCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict[str, Any]:
    p = _product_or_404(db, user, body.product_id)
    brief_id = body.brief_id
    if brief_id is None:
        latest = (
            db.query(UgcBrief)
            .filter(UgcBrief.product_id == p.id, UgcBrief.status == "ready")
            .order_by(UgcBrief.id.desc())
            .first()
        )
        brief_id = latest.id if latest else None
    platform = body.platform or "tiktok"
    preset = get_preset(platform)
    aspect = body.aspect_ratio or preset["aspect_ratio"]
    length = min(int(body.length_sec or 30), int(preset["max_length_sec"]))
    campaign = UgcCampaign(
        user_id=user.id,
        workspace_id=p.workspace_id,
        product_id=p.id,
        brief_id=brief_id,
        name=body.name or f"{p.name or 'Product'} — UGC",
        platform=platform,
        default_aspect_ratio=aspect,
        default_length_sec=length,
        status="draft",
    )
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    return _campaign_out(campaign, db)


@router.get("/campaigns")
def list_campaigns(
    workspace_id: Optional[int] = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    ws = _resolve_workspace(db, user, workspace_id)
    rows = (
        db.query(UgcCampaign)
        .filter(UgcCampaign.workspace_id == ws.id)
        .order_by(UgcCampaign.id.desc())
        .limit(100)
        .all()
    )
    return {"campaigns": [_campaign_out(c, db) for c in rows], "workspace_id": ws.id}


@router.get("/campaigns/{campaign_id}")
def get_campaign(
    campaign_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict[str, Any]:
    c = _campaign_or_404(db, user, campaign_id)
    return _campaign_out(c, db)


@router.post("/campaigns/{campaign_id}/generate")
def generate_variations(
    campaign_id: int,
    body: GenerateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    c = _campaign_or_404(db, user, campaign_id)
    try:
        created = build_variations(db, c, body.count, body.dimensions)
    except InsufficientCreditsError as exc:
        raise HTTPException(
            status_code=402,
            detail={"error": "insufficient_credits", "needed": exc.needed, "available": exc.available},
        )

    _inline_render_if_no_redis(created)

    rows = db.query(UgcVariation).filter(UgcVariation.campaign_id == c.id).order_by(UgcVariation.id.desc()).all()
    return {
        "campaign_id": c.id,
        "created": len(created),
        "credits_remaining": ugc_credits.balance(db, c.workspace_id),
        "variations": [_variation_out(v) for v in rows],
    }


@router.get("/campaigns/{campaign_id}/variations")
def list_variations(
    campaign_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict[str, Any]:
    c = _campaign_or_404(db, user, campaign_id)
    rows = (
        db.query(UgcVariation)
        .filter(UgcVariation.campaign_id == c.id)
        .order_by(UgcVariation.id.desc())
        .all()
    )
    return {"campaign_id": c.id, "variations": [_variation_out(v) for v in rows]}


# --- variations ------------------------------------------------------------


@router.get("/variations/{variation_id}")
def get_variation(
    variation_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict[str, Any]:
    v, _ = _variation_or_404(db, user, variation_id)
    return _variation_out(v)


@router.post("/variations/{variation_id}/render")
def rerender_variation(
    variation_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict[str, Any]:
    v, c = _variation_or_404(db, user, variation_id)
    cost = ugc_credits.credit_cost_per_variation()
    if not ugc_credits.reserve(db, c.workspace_id, cost):
        raise HTTPException(status_code=402, detail={"error": "insufficient_credits", "needed": cost})
    v.status = "queued"
    v.render_error = None
    operation_id = secrets.token_hex(10)
    emit_once(
        db,
        "feature_started",
        event_id=f"feature:ugc-render:variation:{v.id}:operation:{operation_id}:started",
        user=user,
        workspace_id=c.workspace_id,
        properties={
            "feature_key": "ugc_render",
            "campaign_id": c.id,
            "variation_id": v.id,
            "operation_id": operation_id,
            "render_mode": "rerender",
            "result": "queued",
        },
    )
    db.commit()
    job_id = enqueue_ugc_render_job(v.id, "ugc_render", operation_id)
    if job_id:
        v.rq_job_id = job_id
        db.commit()
    elif _no_redis():
        from app.jobs.ugc_render import ugc_render_job

        ugc_render_job(v.id, "ugc_render", operation_id)
        db.refresh(v)
    else:
        v.status = "failed"
        v.render_error = "Could not queue render."
        ugc_credits.refund(
            db,
            c.workspace_id,
            cost,
            variation_id=v.id,
        )
        emit_once(
            db,
            "feature_failed",
            event_id=f"feature:ugc-render:variation:{v.id}:operation:{operation_id}:queue-failed",
            user=user,
            workspace_id=c.workspace_id,
            properties={
                "feature_key": "ugc_render",
                "campaign_id": c.id,
                "variation_id": v.id,
                "operation_id": operation_id,
                "failure_class": "queue",
                "error_code": "queue_unavailable",
                "result": "failure",
            },
        )
        db.commit()
    return _variation_out(v)


@router.post("/variations/{variation_id}/regenerate")
def regenerate_variation(
    variation_id: int,
    body: RegenerateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    v, c = _variation_or_404(db, user, variation_id)
    for field in ("hook", "script", "cta", "angle", "provider_avatar_id", "provider_voice_id", "aspect_ratio"):
        val = getattr(body, field)
        if val is not None:
            setattr(v, field, val)
    if body.length_sec is not None:
        v.length_sec = max(3, min(int(body.length_sec), 60))
    cost = ugc_credits.credit_cost_per_variation()
    if not ugc_credits.reserve(db, c.workspace_id, cost):
        raise HTTPException(status_code=402, detail={"error": "insufficient_credits", "needed": cost})
    v.status = "queued"
    v.render_error = None
    operation_id = secrets.token_hex(10)
    emit_once(
        db,
        "feature_started",
        event_id=f"feature:ugc-regenerate:variation:{v.id}:operation:{operation_id}:started",
        user=user,
        workspace_id=c.workspace_id,
        properties={
            "feature_key": "ugc_regenerate",
            "campaign_id": c.id,
            "variation_id": v.id,
            "operation_id": operation_id,
            "result": "queued",
        },
    )
    db.commit()
    job_id = enqueue_ugc_render_job(v.id, "ugc_regenerate", operation_id)
    if job_id:
        v.rq_job_id = job_id
        db.commit()
    elif _no_redis():
        from app.jobs.ugc_render import ugc_render_job

        ugc_render_job(v.id, "ugc_regenerate", operation_id)
        db.refresh(v)
    else:
        v.status = "failed"
        v.render_error = "Could not queue regeneration."
        ugc_credits.refund(
            db,
            c.workspace_id,
            cost,
            variation_id=v.id,
        )
        emit_once(
            db,
            "feature_failed",
            event_id=f"feature:ugc-regenerate:variation:{v.id}:operation:{operation_id}:queue-failed",
            user=user,
            workspace_id=c.workspace_id,
            properties={
                "feature_key": "ugc_regenerate",
                "campaign_id": c.id,
                "variation_id": v.id,
                "operation_id": operation_id,
                "failure_class": "queue",
                "error_code": "queue_unavailable",
                "result": "failure",
            },
        )
        db.commit()
    return _variation_out(v)


@router.post("/variations/{variation_id}/performance")
def add_performance(
    variation_id: int,
    body: PerformanceCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    v, _ = _variation_or_404(db, user, variation_id)
    row = UgcPerformance(
        variation_id=v.id,
        source=body.source or "manual",
        spend=body.spend,
        impressions=body.impressions,
        clicks=body.clicks,
        ctr=body.ctr,
        conversions=body.conversions,
        cvr=body.cvr,
        roas=body.roas,
        notes=body.notes,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "variation_id": row.variation_id, "captured_at": _iso(row.captured_at)}


# --- catalog + credits -----------------------------------------------------


def _active_provider_name() -> str:
    if os.getenv("UGC_RENDER_DRY_RUN", "").strip().lower() in ("1", "true", "yes"):
        return "stub"
    return (os.getenv("UGC_AVATAR_PROVIDER", "stub") or "stub").strip().lower()


def _avatar_dict(provider: str, a: Any) -> dict[str, Any]:
    return {
        "provider": provider,
        "provider_avatar_id": a.provider_avatar_id,
        "name": a.name,
        "thumbnail_url": a.thumbnail_url,
        "age_range": a.age_range,
        "gender_presentation": a.gender_presentation,
        "region": a.region,
        "default_voice_id": a.default_voice_id,
        "accent": a.accent,
        "energy": a.energy,
        "is_premium": a.is_premium,
    }


def _voice_dict(provider: str, v: Any) -> dict[str, Any]:
    return {
        "provider": provider,
        "provider_voice_id": v.provider_voice_id,
        "name": v.name,
        "gender": v.gender,
        "accent": v.accent,
        "language": v.language,
        "preview_url": getattr(v, "preview_url", None),
        "is_premium": v.is_premium,
    }


@router.get("/avatars")
def list_avatars(
    provider: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    prov = (provider or _active_provider_name()).lower()
    rows = (
        db.query(UgcAvatar)
        .filter(UgcAvatar.is_active.is_(True), UgcAvatar.provider == prov)
        .order_by(UgcAvatar.id.asc())
        .all()
    )
    if rows:
        return {"avatars": [_avatar_dict(a.provider, a) for a in rows]}
    # No persisted catalog for this provider — fetch live (e.g. HeyGen).
    try:
        specs = get_avatar_provider(prov).list_avatars()
    except Exception as exc:  # noqa: BLE001
        return {"avatars": [], "provider": prov, "error": str(exc)}
    return {"avatars": [_avatar_dict(prov, s) for s in specs]}


@router.get("/voices")
def list_voices(
    provider: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    prov = (provider or _active_provider_name()).lower()
    rows = (
        db.query(UgcVoice)
        .filter(UgcVoice.provider == prov)
        .order_by(UgcVoice.id.asc())
        .all()
    )
    if rows:
        return {"voices": [_voice_dict(v.provider, v) for v in rows]}
    try:
        specs = get_avatar_provider(prov).list_voices()
    except Exception as exc:  # noqa: BLE001
        return {"voices": [], "provider": prov, "error": str(exc)}
    return {"voices": [_voice_dict(prov, s) for s in specs]}


@router.post("/providers/{name}/sync")
def sync_provider(
    name: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict[str, Any]:
    """Persist a provider's live avatar/voice catalog into the DB."""
    try:
        return sync_provider_catalog(db, name)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"catalog sync failed: {exc}")


@router.get("/credits")
def get_credits(
    workspace_id: Optional[int] = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    ws = _resolve_workspace(db, user, workspace_id)
    ugc_credits.ensure_monthly_grant(db, ws.id)
    owner = db.query(User).filter(User.id == ws.owner_user_id).first()
    spec = get_plan_spec(owner.plan if owner else None)
    return {
        "workspace_id": ws.id,
        "balance": ugc_credits.balance(db, ws.id),
        "cost_per_variation": ugc_credits.credit_cost_per_variation(),
        "monthly_allotment": spec.ugc_credits_monthly,
        "plan": spec.key,
    }


# --- platforms + performance learner --------------------------------------


@router.get("/platforms")
def get_platforms(user: User = Depends(get_current_user)) -> dict[str, Any]:
    return {"platforms": list_presets()}


@router.get("/campaigns/{campaign_id}/insights")
def campaign_insights(
    campaign_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict[str, Any]:
    c = _campaign_or_404(db, user, campaign_id)
    return analyze_campaign(db, c.id)


@router.post("/campaigns/{campaign_id}/generate-like-top")
def generate_like_top(
    campaign_id: int,
    body: GenerateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    c = _campaign_or_404(db, user, campaign_id)
    analysis = analyze_campaign(db, c.id)
    if not analysis.get("has_data"):
        raise HTTPException(
            status_code=400,
            detail={"error": "no_performance_data", "message": analysis.get("insights")},
        )
    dims = analysis["recommended_dimensions"]
    try:
        created = build_variations(db, c, body.count, dims)
    except InsufficientCreditsError as exc:
        raise HTTPException(
            status_code=402,
            detail={"error": "insufficient_credits", "needed": exc.needed, "available": exc.available},
        )
    _inline_render_if_no_redis(created)
    rows = db.query(UgcVariation).filter(UgcVariation.campaign_id == c.id).order_by(UgcVariation.id.desc()).all()
    return {
        "campaign_id": c.id,
        "created": len(created),
        "credits_remaining": ugc_credits.balance(db, c.workspace_id),
        "recommended_dimensions": dims,
        "insights": analysis.get("insights"),
        "variations": [_variation_out(v) for v in rows],
    }


# --- provider webhook (no auth; signature-verified) ------------------------


@public_router.post("/providers/{name}/webhook")
async def provider_webhook(
    name: str, request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    secret = os.environ.get("UGC_PROVIDER_WEBHOOK_SECRET", "").strip()
    if secret:
        provided = request.headers.get("x-ugc-signature", "") or request.query_params.get("secret", "")
        if provided != secret:
            raise HTTPException(status_code=401, detail="bad signature")
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    job_id = str(payload.get("provider_job_id") or payload.get("video_id") or "")
    status = str(payload.get("status") or "")
    if not job_id:
        return {"ok": True, "ignored": "no provider_job_id"}
    var = db.query(UgcVariation).filter(UgcVariation.provider_job_id == job_id).first()
    if not var:
        return {"ok": True, "ignored": "unknown job"}
    # The render job owns assembly; the webhook just records terminal failures
    # so a stuck poll can be short-circuited.
    if status in ("failed", "error"):
        var.status = "failed"
        var.render_error = str(payload.get("error") or "provider reported failure")[:4000]
        db.commit()
    logger.info("ugc provider webhook (%s) for variation %s: %s", name, var.id, status)
    return {"ok": True}
