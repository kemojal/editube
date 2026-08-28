"""Public tracking, partner portal, and guarded affiliate operations."""

from __future__ import annotations

import csv
import io
import logging
import os
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import (
    AffiliateApplication,
    AffiliateAttribution,
    AffiliateAuditEvent,
    AffiliateCampaign,
    AffiliateClick,
    AffiliateComplianceProfile,
    AffiliateCommissionEntry,
    AffiliateCommissionState,
    AffiliatePartner,
    AffiliatePayout,
    AffiliatePayoutItem,
    AffiliateProgramTerms,
    AffiliateLaunchApproval,
    AffiliateTermsAcceptance,
    User,
    UserMFAMethod,
)
from app.services.affiliate_program import (
    PROGRAM_TERMS_CHECKSUM,
    SUPPORTED_CHANNELS,
    AffiliateProgramError,
    accept_partner_terms,
    active_terms,
    affiliate_invoice_decision_exists,
    approve_payout,
    audit,
    build_partner_link,
    campaign_payload,
    claim_attribution,
    compliance_payload,
    create_partner_campaign,
    create_payout_batch,
    create_terms_draft,
    execute_payout,
    launch_approval_state,
    legal_launch_ready,
    list_partner_campaigns,
    latest_terms,
    partner_dashboard,
    partner_for_user,
    payable_balance,
    payouts_runtime_enabled,
    publish_terms,
    record_launch_approval,
    record_click,
    record_manual_adjustment,
    record_paid_invoice,
    revoke_launch_approval,
    review_application,
    submit_application,
    supported_countries,
    terms_payload,
    update_partner_campaign,
    update_partner_compliance,
    utcnow,
    withdraw_application,
    user_has_active_affiliate_attribution,
)
from app.services.affiliate_reconciliation import (
    database_reconciliation_report,
    stripe_reconciliation_report,
)
from app.services.affiliate_stripe import (
    invoice_with_complete_lines,
    stripe_field,
    user_for_invoice,
)
from app.utils.security import get_current_user

try:
    import stripe
except Exception:  # pragma: no cover - startup stays healthy without optional SDK
    stripe = None

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/affiliates", tags=["Affiliates"])
public_router = APIRouter(prefix="/public/affiliates", tags=["Affiliates-Public"])
admin_router = APIRouter(prefix="/admin/affiliates", tags=["Affiliates-Admin"])


def _raise(exc: AffiliateProgramError) -> None:
    status = {
        "not_found": 404,
        "invalid_link": 404,
        "invalid_campaign": 404,
        "click_expired": 410,
        "click_rate_limited": 429,
        "application_rate_limited": 429,
        "applications_closed": 409,
        "already_partner": 409,
        "acquisition_conflict": 409,
        "application_pending": 409,
        "already_reviewed": 409,
        "invalid_state": 409,
        "below_threshold": 409,
        "balance_changed": 409,
        "partner_held": 409,
        "partner_inactive": 409,
        "connect_incomplete": 409,
        "terms_changed": 409,
        "terms_unavailable": 409,
        "unsupported_country": 409,
        "copy_mismatch": 409,
        "legal_not_approved": 409,
        "launch_approvals_missing": 409,
        "separation_required": 409,
        "approval_exists": 409,
        "compliance_incomplete": 409,
        "compliance_changed": 409,
        "duplicate_campaign": 409,
        "campaign_limit": 409,
        "payouts_disabled": 503,
        "stripe_unavailable": 503,
        "transfer_failed": 502,
    }.get(exc.reason, 400)
    raise HTTPException(status_code=status, detail=exc.message) from exc


def _require_admin(user: User) -> None:
    if (user.role or "").strip().lower() != "admin":
        raise HTTPException(status_code=403, detail="Affiliate administration is internal-only")


def _require_finance_admin(db: Session, user: User) -> None:
    _require_admin(user)
    verified = (
        db.query(UserMFAMethod.id)
        .filter(
            UserMFAMethod.user_id == user.id,
            UserMFAMethod.verified_at.isnot(None),
            UserMFAMethod.disabled_at.is_(None),
        )
        .first()
    )
    if not verified:
        raise HTTPException(
            status_code=403,
            detail="Verified two-factor authentication is required for affiliate financial operations",
        )


def _partner_or_404(db: Session, user: User) -> AffiliatePartner:
    partner = partner_for_user(db, user.id)
    if not partner:
        raise HTTPException(status_code=404, detail="No affiliate partner profile exists for this account")
    return partner


def _safe_internal_path(raw: str | None, default: str = "/partners/affiliate/dashboard") -> str:
    value = (raw or "").strip()
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return default
    return value[:500]


def _value(obj, key: str, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


class ClickBody(BaseModel):
    campaign: str | None = Field(default=None, max_length=120)
    landing_path: str | None = Field(default="/signup", max_length=500)
    referrer: str | None = Field(default=None, max_length=1000)


class ClaimBody(BaseModel):
    click_token: str = Field(min_length=20, max_length=200)


class ApplicationBody(BaseModel):
    display_name: str = Field(min_length=2, max_length=120)
    business_name: str | None = Field(default=None, max_length=160)
    website_url: str | None = Field(default=None, max_length=500)
    country_code: str = Field(min_length=2, max_length=2)
    audience_description: str = Field(min_length=80, max_length=2000)
    audience_size: int | None = Field(default=None, ge=0, le=2_000_000_000)
    promotion_channels: list[str] = Field(min_length=1, max_length=8)
    attested: bool

    @field_validator("country_code")
    @classmethod
    def uppercase_country(cls, value: str) -> str:
        return value.strip().upper()


class AcceptTermsBody(BaseModel):
    version: str = Field(min_length=1, max_length=50)
    checksum: str = Field(min_length=32, max_length=128)
    accepted: bool


class ReviewApplicationBody(BaseModel):
    decision: str
    notes: str | None = Field(default=None, max_length=4000)


class TermsDraftBody(BaseModel):
    version: str = Field(min_length=1, max_length=50, pattern=r"^[A-Za-z0-9._-]+$")
    commission_rate_bps: int = Field(ge=0, le=10_000)
    commission_months: int = Field(ge=1, le=60)
    attribution_window_days: int = Field(ge=1, le=365)
    payout_minimum_minor: int = Field(ge=1, le=100_000_000)
    hold_days: int = Field(ge=0, le=180)


class PartnerUpdateBody(BaseModel):
    status: str | None = None
    risk_status: str | None = None
    hold_reason: str | None = Field(default=None, max_length=2000)
    risk_review_note: str | None = Field(default=None, max_length=2000)
    custom_commission_rate_bps: int | None = Field(default=None, ge=0, le=10_000)
    custom_commission_months: int | None = Field(default=None, ge=1, le=60)
    terms_version_id: int | None = Field(default=None, ge=1)


class PayoutCreateBody(BaseModel):
    partner_id: int
    through: datetime | None = None


class ManualAdjustmentBody(BaseModel):
    partner_id: int
    attribution_id: int
    amount_minor: int = Field(ge=-100_000_000, le=100_000_000)
    reason: str = Field(min_length=20, max_length=1000)
    idempotency_key: str = Field(min_length=12, max_length=120)


class ReconcileInvoiceBody(BaseModel):
    invoice_id: str = Field(min_length=5, max_length=255, pattern=r"^in_[A-Za-z0-9_]+$")
    apply: bool = False


class CampaignCreateBody(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    slug: str = Field(min_length=1, max_length=50)
    destination_path: str = Field(default="/signup", min_length=1, max_length=500)


class CampaignUpdateBody(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    destination_path: str | None = Field(default=None, min_length=1, max_length=500)
    status: str | None = None

    @model_validator(mode="after")
    def require_change(self):
        if self.name is None and self.destination_path is None and self.status is None:
            raise ValueError("At least one campaign field is required")
        return self


class LaunchApprovalBody(BaseModel):
    role: str
    note: str = Field(min_length=20, max_length=4000)


class ComplianceBody(BaseModel):
    tax_residency_country: str = Field(min_length=2, max_length=2)
    tax_form_type: str
    tax_form_reference: str | None = Field(default=None, min_length=6, max_length=500)
    tax_verified: bool
    sanctions_status: str
    withholding_rate_bps: int = Field(ge=0, le=10_000)
    review_note: str = Field(min_length=20, max_length=4000)


def _application_payload(application: AffiliateApplication) -> dict:
    return {
        "id": application.id,
        "user_id": application.user_id,
        "email": application.email,
        "display_name": application.display_name,
        "business_name": application.business_name,
        "website_url": application.website_url,
        "country_code": application.country_code,
        "audience_description": application.audience_description,
        "audience_size": application.audience_size,
        "promotion_channels": application.promotion_channels or [],
        "payout_currency": application.payout_currency,
        "status": application.status,
        "applicant_attested_at": application.applicant_attested_at,
        "reviewed_by_user_id": application.reviewed_by_user_id,
        "review_notes": application.review_notes,
        "reviewed_at": application.reviewed_at,
        "created_at": application.created_at,
        "updated_at": application.updated_at,
    }


def _partner_payload(db: Session, partner: AffiliatePartner) -> dict:
    terms = db.query(AffiliateProgramTerms).filter(AffiliateProgramTerms.id == partner.terms_version_id).first()
    accepted = (
        db.query(AffiliateTermsAcceptance)
        .filter(
            AffiliateTermsAcceptance.partner_id == partner.id,
            AffiliateTermsAcceptance.terms_version_id == partner.terms_version_id,
        )
        .first()
    )
    return {
        "id": partner.id,
        "user_id": partner.user_id,
        "application_id": partner.application_id,
        "code": partner.code,
        "link": build_partner_link(partner.code),
        "status": partner.status,
        "risk_status": partner.risk_status,
        "hold_reason": partner.hold_reason,
        "commission_rate_bps": partner.custom_commission_rate_bps
        if partner.custom_commission_rate_bps is not None
        else (terms.commission_rate_bps if terms else None),
        "commission_months": partner.custom_commission_months
        if partner.custom_commission_months is not None
        else (terms.commission_months if terms else None),
        "custom_commission_rate_bps": partner.custom_commission_rate_bps,
        "custom_commission_months": partner.custom_commission_months,
        "terms": terms_payload(terms, db),
        "terms_accepted_at": accepted.accepted_at if accepted else None,
        "stripe_connect_account_id": partner.stripe_connect_account_id,
        "payouts_enabled": partner.payouts_enabled,
        "compliance": compliance_payload(
            db.query(AffiliateComplianceProfile)
            .filter(AffiliateComplianceProfile.partner_id == partner.id)
            .first()
        ),
        "approved_at": partner.approved_at,
        "suspended_at": partner.suspended_at,
        "closed_at": partner.closed_at,
        "created_at": partner.created_at,
        "updated_at": partner.updated_at,
    }


def _entry_payload(entry: AffiliateCommissionEntry) -> dict:
    return {
        "id": entry.id,
        "entry_type": entry.entry_type,
        "stripe_invoice_id": entry.stripe_invoice_id,
        "amount_minor": entry.amount_minor,
        "commissionable_minor": entry.commissionable_minor,
        "rate_bps": entry.rate_bps,
        "currency": entry.currency,
        "available_at": entry.available_at,
        "description": entry.description,
        "created_at": entry.created_at,
    }


def _csv_response(rows: list[list], filename: str) -> StreamingResponse:
    def safe_cell(value):
        # Spreadsheet apps execute cells beginning with these characters as
        # formulas. CSV exports can contain operator-written descriptions, so
        # neutralize them without changing the underlying ledger.
        if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + value
        return value

    buffer = io.StringIO()
    csv.writer(buffer).writerows([[safe_cell(value) for value in row] for row in rows])
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _payout_statement_rows(db: Session, payout: AffiliatePayout) -> list[list]:
    items = (
        db.query(AffiliatePayoutItem, AffiliateCommissionEntry)
        .join(
            AffiliateCommissionEntry,
            AffiliateCommissionEntry.id == AffiliatePayoutItem.commission_entry_id,
        )
        .filter(AffiliatePayoutItem.payout_id == payout.id)
        .order_by(AffiliateCommissionEntry.created_at, AffiliateCommissionEntry.id)
        .all()
    )
    rows: list[list] = [[
        "payout_id",
        "payout_status",
        "period_start",
        "period_end",
        "paid_at",
        "transfer_reference",
        "gross_amount_minor",
        "withholding_rate_bps",
        "withholding_minor",
        "net_amount_minor",
        "entry_id",
        "entry_created_at",
        "entry_type",
        "stripe_invoice_id",
        "item_minor",
        "currency",
    ]]
    rows.extend(
        [
            payout.id,
            payout.status,
            payout.period_start.isoformat() if payout.period_start else "",
            payout.period_end.isoformat() if payout.period_end else "",
            payout.paid_at.isoformat() if payout.paid_at else "",
            payout.stripe_transfer_id or "",
            payout.gross_amount_minor,
            payout.withholding_rate_bps,
            payout.withholding_minor,
            payout.amount_minor,
            entry.id,
            entry.created_at.isoformat() if entry.created_at else "",
            entry.entry_type,
            entry.stripe_invoice_id or "",
            item.amount_minor,
            payout.currency,
        ]
        for item, entry in items
    )
    if not items:
        rows.append([
            payout.id,
            payout.status,
            payout.period_start.isoformat() if payout.period_start else "",
            payout.period_end.isoformat() if payout.period_end else "",
            payout.paid_at.isoformat() if payout.paid_at else "",
            payout.stripe_transfer_id or "",
            payout.gross_amount_minor,
            payout.withholding_rate_bps,
            payout.withholding_minor,
            payout.amount_minor,
            "",
            "",
            "",
            "",
            0,
            payout.currency,
        ])
    return rows


def _payout_payload(payout: AffiliatePayout) -> dict:
    return {
        "id": payout.id,
        "partner_id": payout.partner_id,
        "currency": payout.currency,
        "gross_amount_minor": payout.gross_amount_minor,
        "withholding_rate_bps": payout.withholding_rate_bps,
        "withholding_minor": payout.withholding_minor,
        "amount_minor": payout.amount_minor,
        "threshold_minor": payout.threshold_minor,
        "status": payout.status,
        "period_start": payout.period_start,
        "period_end": payout.period_end,
        "stripe_transfer_id": payout.stripe_transfer_id,
        "failure_reason": payout.failure_reason,
        "created_by_user_id": payout.created_by_user_id,
        "approved_by_user_id": payout.approved_by_user_id,
        "approved_at": payout.approved_at,
        "processed_at": payout.processed_at,
        "paid_at": payout.paid_at,
        "created_at": payout.created_at,
    }


@public_router.get("/program")
def get_public_program(db: Session = Depends(get_db)):
    terms = active_terms(db) or latest_terms(db)
    return {
        "terms": terms_payload(terms, db),
        "supported_countries": sorted(supported_countries()),
        "promotion_channels": sorted(SUPPORTED_CHANNELS),
    }


@public_router.post("/click/{code}", status_code=201)
def create_affiliate_click(code: str, body: ClickBody, request: Request, db: Session = Depends(get_db)):
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    ip = forwarded or (request.client.host if request.client else None)
    try:
        click = record_click(
            db,
            code=code,
            campaign=body.campaign,
            landing_path=body.landing_path,
            referrer=body.referrer,
            ip=ip,
            user_agent=request.headers.get("user-agent"),
        )
    except AffiliateProgramError as exc:
        _raise(exc)
    return {"click_token": click.token, "expires_at": click.expires_at}


@router.get("/me")
def get_affiliate_me(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    applications = (
        db.query(AffiliateApplication)
        .filter(AffiliateApplication.user_id == current_user.id)
        .order_by(AffiliateApplication.created_at.desc(), AffiliateApplication.id.desc())
        .all()
    )
    partner = partner_for_user(db, current_user.id)
    return {
        "applications": [_application_payload(item) for item in applications],
        "partner": _partner_payload(db, partner) if partner else None,
        "program": terms_payload(active_terms(db) or latest_terms(db), db),
    }


@router.post("/applications", status_code=201)
def apply_to_affiliate_program(
    body: ApplicationBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        application = submit_application(
            db,
            user=current_user,
            display_name=body.display_name,
            business_name=body.business_name,
            website_url=body.website_url,
            country_code=body.country_code,
            audience_description=body.audience_description,
            audience_size=body.audience_size,
            promotion_channels=body.promotion_channels,
            attested=body.attested,
        )
    except AffiliateProgramError as exc:
        _raise(exc)
    return {"application": _application_payload(application)}


@router.post("/applications/{application_id}/withdraw")
def withdraw_my_affiliate_application(
    application_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        application = withdraw_application(
            db, application_id=application_id, user=current_user
        )
    except AffiliateProgramError as exc:
        _raise(exc)
    return {"application": _application_payload(application)}


@router.post("/claim")
def claim_affiliate(
    body: ClaimBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.onboarding_completed:
        raise HTTPException(status_code=409, detail="Affiliate attribution can only be attached to a new account.")
    try:
        attribution = claim_attribution(db, user=current_user, click_token=body.click_token)
    except AffiliateProgramError as exc:
        _raise(exc)
    return {"applied": True, "attribution_id": attribution.id}


@router.post("/me/terms/accept")
def accept_terms(
    body: AcceptTermsBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not body.accepted:
        raise HTTPException(status_code=400, detail="You must explicitly accept the terms.")
    partner = _partner_or_404(db, current_user)
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    ip = forwarded or (request.client.host if request.client else None)
    try:
        acceptance = accept_partner_terms(
            db,
            partner=partner,
            user=current_user,
            version=body.version,
            checksum=body.checksum,
            ip=ip,
            user_agent=request.headers.get("user-agent"),
        )
    except AffiliateProgramError as exc:
        _raise(exc)
    db.refresh(partner)
    return {"accepted_at": acceptance.accepted_at, "partner": _partner_payload(db, partner)}


@router.get("/me/dashboard")
def get_affiliate_dashboard(
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
):
    partner = _partner_or_404(db, current_user)
    data = partner_dashboard(db, partner)
    return {
        "partner": _partner_payload(db, partner),
        "metrics": data["metrics"],
        "entries": [_entry_payload(entry) for entry in data["entries"]],
        "payouts": [_payout_payload(payout) for payout in data["payouts"]],
        "campaigns": data["campaigns"],
    }


@router.get("/me/campaigns")
def get_my_affiliate_campaigns(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    partner = _partner_or_404(db, current_user)
    return {
        "campaigns": [
            campaign_payload(item, partner.code)
            for item in list_partner_campaigns(db, partner)
        ]
    }


@router.post("/me/campaigns", status_code=201)
def create_my_affiliate_campaign(
    body: CampaignCreateBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    partner = _partner_or_404(db, current_user)
    try:
        campaign = create_partner_campaign(
            db,
            partner=partner,
            name=body.name,
            slug=body.slug,
            destination_path=body.destination_path,
        )
    except AffiliateProgramError as exc:
        _raise(exc)
    return {"campaign": campaign_payload(campaign, partner.code)}


@router.patch("/me/campaigns/{campaign_id}")
def update_my_affiliate_campaign(
    campaign_id: int,
    body: CampaignUpdateBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    partner = _partner_or_404(db, current_user)
    campaign = (
        db.query(AffiliateCampaign)
        .filter(
            AffiliateCampaign.id == campaign_id,
            AffiliateCampaign.partner_id == partner.id,
        )
        .first()
    )
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    try:
        campaign = update_partner_campaign(
            db,
            partner=partner,
            campaign=campaign,
            **body.model_dump(exclude_unset=True),
        )
    except AffiliateProgramError as exc:
        _raise(exc)
    return {"campaign": campaign_payload(campaign, partner.code)}


@router.get("/me/ledger.csv")
def export_my_affiliate_ledger(
    from_ts: datetime | None = Query(default=None),
    to_ts: datetime | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    partner = _partner_or_404(db, current_user)
    query = db.query(AffiliateCommissionEntry).filter(
        AffiliateCommissionEntry.partner_id == partner.id
    )
    if from_ts is not None:
        query = query.filter(AffiliateCommissionEntry.created_at >= from_ts)
    if to_ts is not None:
        query = query.filter(AffiliateCommissionEntry.created_at <= to_ts)
    entries = query.order_by(
        AffiliateCommissionEntry.created_at.asc(),
        AffiliateCommissionEntry.id.asc(),
    ).limit(10_000).all()
    rows: list[list] = [[
        "entry_id",
        "created_at",
        "entry_type",
        "stripe_invoice_id",
        "commissionable_minor",
        "rate_bps",
        "commission_minor",
        "currency",
        "available_at",
        "description",
    ]]
    rows.extend(
        [
            entry.id,
            entry.created_at.isoformat() if entry.created_at else "",
            entry.entry_type,
            entry.stripe_invoice_id or "",
            entry.commissionable_minor,
            entry.rate_bps,
            entry.amount_minor,
            entry.currency,
            entry.available_at.isoformat() if entry.available_at else "",
            entry.description or "",
        ]
        for entry in entries
    )
    return _csv_response(rows, f"affiliate-ledger-{partner.id}.csv")


@router.get("/me/payouts/{payout_id}/statement.csv")
def export_my_affiliate_payout_statement(
    payout_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    partner = _partner_or_404(db, current_user)
    payout = (
        db.query(AffiliatePayout)
        .filter(AffiliatePayout.id == payout_id, AffiliatePayout.partner_id == partner.id)
        .first()
    )
    if not payout:
        raise HTTPException(status_code=404, detail="Payout statement not found")
    return _csv_response(
        _payout_statement_rows(db, payout),
        f"affiliate-payout-{payout.id}.csv",
    )


@router.post("/me/connect/account")
def create_connect_account(
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
):
    partner = (
        db.query(AffiliatePartner)
        .filter(
            AffiliatePartner.user_id == current_user.id,
        )
        .with_for_update()
        .first()
    )
    if not partner:
        raise HTTPException(status_code=404, detail="No affiliate partner profile exists for this account")
    if partner.status not in {"pending_terms", "active"}:
        raise HTTPException(status_code=409, detail="Partner profile is not eligible for payout onboarding")
    if partner.stripe_connect_account_id:
        return {"stripe_connect_account_id": partner.stripe_connect_account_id, "created": False}
    if stripe is None or not os.getenv("STRIPE_SECRET_KEY"):
        raise HTTPException(status_code=503, detail="Stripe is not configured")
    application = db.query(AffiliateApplication).filter(AffiliateApplication.id == partner.application_id).first()
    if not application or application.country_code not in supported_countries():
        raise HTTPException(status_code=409, detail="This payout country is not enabled")
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
    try:
        account = stripe.Account.create(
            type="express",
            country=application.country_code if application else "US",
            email=current_user.email,
            capabilities={"transfers": {"requested": True}},
            metadata={"affiliate_partner_id": str(partner.id), "user_id": str(current_user.id)},
            idempotency_key=f"affiliate-connect-{partner.id}",
        )
    except Exception:
        logger.exception("Affiliate Connect account creation failed partner=%s", partner.id)
        raise HTTPException(status_code=502, detail="Could not create the payout account") from None
    partner.stripe_connect_account_id = str(_value(account, "id"))
    audit(
        db,
        "connect.created",
        actor_user_id=current_user.id,
        partner_id=partner.id,
        source_ref=partner.stripe_connect_account_id,
    )
    db.commit()
    return {"stripe_connect_account_id": partner.stripe_connect_account_id, "created": True}


@router.post("/me/connect/account-link")
def create_connect_account_link(
    return_path: str = Query(default="/partners/affiliate/dashboard", max_length=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    partner = _partner_or_404(db, current_user)
    if not partner.stripe_connect_account_id:
        raise HTTPException(status_code=409, detail="Create the payout account first")
    if stripe is None or not os.getenv("STRIPE_SECRET_KEY"):
        raise HTTPException(status_code=503, detail="Stripe is not configured")
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
    base = os.getenv("FRONTEND_BASE_URL", "http://localhost:3000").rstrip("/")
    path = _safe_internal_path(return_path)
    try:
        link = stripe.AccountLink.create(
            account=partner.stripe_connect_account_id,
            refresh_url=f"{base}{path}?connect=refresh",
            return_url=f"{base}{path}?connect=return",
            type="account_onboarding",
        )
    except Exception:
        logger.exception("Affiliate Connect account-link failed partner=%s", partner.id)
        raise HTTPException(status_code=502, detail="Could not start payout verification") from None
    return {"url": str(_value(link, "url"))}


@router.get("/me/connect/status")
def get_connect_status(
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
):
    partner = _partner_or_404(db, current_user)
    base = {
        "stripe_connect_account_id": partner.stripe_connect_account_id,
        "details_submitted": False,
        "payouts_enabled": partner.payouts_enabled,
    }
    if not partner.stripe_connect_account_id:
        return base
    if stripe is None or not os.getenv("STRIPE_SECRET_KEY"):
        return base
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
    try:
        account = stripe.Account.retrieve(partner.stripe_connect_account_id)
    except Exception:
        logger.exception("Affiliate Connect status failed partner=%s", partner.id)
        raise HTTPException(status_code=502, detail="Could not load payout verification status") from None
    details_submitted = bool(_value(account, "details_submitted"))
    payouts_enabled = bool(_value(account, "payouts_enabled"))
    if partner.payouts_enabled != payouts_enabled:
        partner.payouts_enabled = payouts_enabled
        audit(
            db,
            "connect.status_changed",
            actor_user_id=current_user.id,
            partner_id=partner.id,
            payload={"details_submitted": details_submitted, "payouts_enabled": payouts_enabled},
        )
        db.commit()
    return {
        "stripe_connect_account_id": partner.stripe_connect_account_id,
        "details_submitted": details_submitted,
        "payouts_enabled": payouts_enabled,
    }


@admin_router.get("/terms")
def admin_list_terms(
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
):
    _require_admin(current_user)
    terms = db.query(AffiliateProgramTerms).order_by(AffiliateProgramTerms.created_at.desc()).all()
    return {
        "terms": [
            {
                **terms_payload(item, db),
                "launch_approvals": launch_approval_state(db, item),
            }
            for item in terms
        ],
        "deployed_checksum": PROGRAM_TERMS_CHECKSUM,
    }


@admin_router.get("/launch-readiness")
def admin_affiliate_launch_readiness(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    terms = active_terms(db) or latest_terms(db)
    approvals = launch_approval_state(db, terms)
    checks = {
        "terms_present": terms is not None,
        "terms_active": bool(terms and terms.status == "active"),
        "legal_environment_approved": legal_launch_ready(),
        "role_approvals_complete": approvals["ready"],
        "supported_countries_configured": bool(supported_countries()),
        "stripe_configured": bool(os.getenv("STRIPE_SECRET_KEY")),
        "reconciliation_alert_configured": bool(os.getenv("AFFILIATE_ALERT_WEBHOOK_URL")),
        "payout_execution_enabled": payouts_runtime_enabled(),
    }
    blockers = [name for name, passed in checks.items() if not passed]
    return {
        "ready_for_applications": all(
            checks[name]
            for name in (
                "terms_present",
                "terms_active",
                "legal_environment_approved",
                "role_approvals_complete",
                "supported_countries_configured",
            )
        ),
        "ready_for_payout_execution": not blockers,
        "checks": checks,
        "blockers": blockers,
        "supported_countries": sorted(supported_countries()),
        "terms": terms_payload(terms, db),
        "launch_approvals": approvals,
    }


@admin_router.post("/terms", status_code=201)
def admin_create_terms(
    body: TermsDraftBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    try:
        terms = create_terms_draft(db, admin=current_user, **body.model_dump())
    except AffiliateProgramError as exc:
        _raise(exc)
    return {"terms": terms_payload(terms, db)}


@admin_router.post("/terms/{terms_id}/approvals", status_code=201)
def admin_record_launch_approval(
    terms_id: int,
    body: LaunchApprovalBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_finance_admin(db, current_user)
    terms = db.query(AffiliateProgramTerms).filter(AffiliateProgramTerms.id == terms_id).first()
    if not terms:
        raise HTTPException(status_code=404, detail="Terms not found")
    try:
        approval = record_launch_approval(
            db,
            terms=terms,
            role=body.role,
            admin=current_user,
            note=body.note,
        )
    except AffiliateProgramError as exc:
        _raise(exc)
    return {
        "approval": {
            "id": approval.id,
            "role": approval.approval_role,
            "approved_by_user_id": approval.approved_by_user_id,
            "approved_at": approval.approved_at,
            "note": approval.note,
        },
        "launch_approvals": launch_approval_state(db, terms),
    }


@admin_router.post("/launch-approvals/{approval_id}/revoke")
def admin_revoke_launch_approval(
    approval_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_finance_admin(db, current_user)
    approval = (
        db.query(AffiliateLaunchApproval)
        .filter(AffiliateLaunchApproval.id == approval_id)
        .first()
    )
    if not approval:
        raise HTTPException(status_code=404, detail="Launch approval not found")
    terms = (
        db.query(AffiliateProgramTerms)
        .filter(AffiliateProgramTerms.id == approval.terms_version_id)
        .first()
    )
    try:
        revoke_launch_approval(db, approval=approval, admin=current_user)
    except AffiliateProgramError as exc:
        _raise(exc)
    return {"launch_approvals": launch_approval_state(db, terms)}


@admin_router.post("/terms/{terms_id}/publish")
def admin_publish_terms(
    terms_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_finance_admin(db, current_user)
    terms = db.query(AffiliateProgramTerms).filter(AffiliateProgramTerms.id == terms_id).first()
    if not terms:
        raise HTTPException(status_code=404, detail="Terms not found")
    try:
        terms = publish_terms(db, terms, current_user)
    except AffiliateProgramError as exc:
        _raise(exc)
    return {"terms": terms_payload(terms, db)}


@admin_router.get("/applications")
def admin_list_applications(
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    query = db.query(AffiliateApplication)
    if status:
        query = query.filter(AffiliateApplication.status == status)
    applications = query.order_by(AffiliateApplication.created_at.desc()).limit(limit).all()
    return {"applications": [_application_payload(item) for item in applications]}


@admin_router.post("/applications/{application_id}/review")
def admin_review_application(
    application_id: int,
    body: ReviewApplicationBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    application = db.query(AffiliateApplication).filter(AffiliateApplication.id == application_id).first()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    try:
        application, partner = review_application(
            db,
            application=application,
            admin=current_user,
            decision=body.decision,
            notes=body.notes,
        )
    except AffiliateProgramError as exc:
        _raise(exc)
    return {
        "application": _application_payload(application),
        "partner": _partner_payload(db, partner) if partner else None,
    }


@admin_router.get("/partners")
def admin_list_partners(
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    query = db.query(AffiliatePartner)
    if status:
        query = query.filter(AffiliatePartner.status == status)
    partners = query.order_by(AffiliatePartner.created_at.desc()).limit(500).all()
    return {"partners": [_partner_payload(db, partner) for partner in partners]}


@admin_router.get("/partners/{partner_id}/attributions")
def admin_list_attributions(
    partner_id: int,
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    rows = (
        db.query(AffiliateAttribution, User, AffiliateClick)
        .join(User, User.id == AffiliateAttribution.invitee_user_id)
        .join(AffiliateClick, AffiliateClick.id == AffiliateAttribution.click_id)
        .filter(AffiliateAttribution.partner_id == partner_id)
        .order_by(AffiliateAttribution.attributed_at.desc())
        .limit(limit)
        .all()
    )
    return {
        "attributions": [
            {
                "id": attribution.id,
                "invitee_user_id": attribution.invitee_user_id,
                "invitee_email": user.email,
                "status": attribution.status,
                "void_reason": attribution.void_reason,
                "risk_flags": attribution.risk_flags or [],
                "campaign": click.campaign,
                "attributed_at": attribution.attributed_at,
                "first_paid_at": attribution.first_paid_at,
                "commission_ends_at": attribution.commission_ends_at,
            }
            for attribution, user, click in rows
        ]
    }


@admin_router.get("/partners/{partner_id}/ledger")
def admin_partner_ledger(
    partner_id: int,
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    partner = db.query(AffiliatePartner).filter(AffiliatePartner.id == partner_id).first()
    if not partner:
        raise HTTPException(status_code=404, detail="Partner not found")
    entries = (
        db.query(AffiliateCommissionEntry)
        .filter(AffiliateCommissionEntry.partner_id == partner_id)
        .order_by(
            AffiliateCommissionEntry.created_at.desc(),
            AffiliateCommissionEntry.id.desc(),
        )
        .limit(limit)
        .all()
    )
    return {
        "entries": [_entry_payload(entry) for entry in entries],
        "payable_minor": payable_balance(db, partner_id),
        "currency": partner.terms.currency if partner.terms else "usd",
    }


@admin_router.patch("/partners/{partner_id}")
def admin_update_partner(
    partner_id: int,
    body: PartnerUpdateBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_finance_admin(db, current_user)
    partner = (
        db.query(AffiliatePartner)
        .filter(AffiliatePartner.id == partner_id)
        .with_for_update()
        .first()
    )
    if not partner:
        raise HTTPException(status_code=404, detail="Partner not found")
    updates = body.model_dump(exclude_unset=True)
    previous_risk_status = partner.risk_status
    risk_review_note = (updates.pop("risk_review_note", None) or "").strip()
    if "status" in updates:
        status = updates["status"]
        if status not in {"active", "suspended", "closed"}:
            raise HTTPException(status_code=400, detail="Invalid partner status")
        if partner.status == "closed" and status != "closed":
            raise HTTPException(status_code=409, detail="A closed partner profile cannot be reopened")
        if status == "active":
            acceptance = (
                db.query(AffiliateTermsAcceptance.id)
                .filter(
                    AffiliateTermsAcceptance.partner_id == partner.id,
                    AffiliateTermsAcceptance.terms_version_id == partner.terms_version_id,
                )
                .first()
            )
            if not acceptance:
                raise HTTPException(status_code=409, detail="Partner has not accepted the assigned terms")
        partner.status = status
        partner.suspended_at = utcnow() if status == "suspended" else None
        partner.closed_at = utcnow() if status == "closed" else None
    if "risk_status" in updates:
        if updates["risk_status"] not in {"clear", "review", "held"}:
            raise HTTPException(status_code=400, detail="Invalid risk status")
        if (
            updates["risk_status"] == "clear"
            and previous_risk_status != "clear"
            and len(risk_review_note) < 20
        ):
            raise HTTPException(
                status_code=400,
                detail="Clearing a risk review requires a 20-character resolution note",
            )
        partner.risk_status = updates["risk_status"]
    if "hold_reason" in updates:
        partner.hold_reason = (updates["hold_reason"] or "").strip() or None
    if "terms_version_id" in updates and updates["terms_version_id"] != partner.terms_version_id:
        assigned_terms = (
            db.query(AffiliateProgramTerms)
            .filter(
                AffiliateProgramTerms.id == updates["terms_version_id"],
                AffiliateProgramTerms.status == "active",
            )
            .first()
        )
        if not assigned_terms:
            raise HTTPException(status_code=409, detail="Only active terms can be assigned")
        if partner.status == "closed":
            raise HTTPException(status_code=409, detail="A closed partner profile cannot receive new terms")
        partner.terms_version_id = assigned_terms.id
        partner.status = "pending_terms"
        if (
            partner.custom_commission_rate_bps is not None
            and partner.custom_commission_rate_bps < assigned_terms.commission_rate_bps
        ):
            partner.custom_commission_rate_bps = None
        if (
            partner.custom_commission_months is not None
            and partner.custom_commission_months < assigned_terms.commission_months
        ):
            partner.custom_commission_months = None
    assigned_terms = (
        db.query(AffiliateProgramTerms)
        .filter(AffiliateProgramTerms.id == partner.terms_version_id)
        .first()
    )
    if not assigned_terms:
        raise HTTPException(status_code=409, detail="Assigned partner terms are unavailable")
    if "custom_commission_rate_bps" in updates:
        custom_rate = updates["custom_commission_rate_bps"]
        if custom_rate is not None and custom_rate < assigned_terms.commission_rate_bps:
            raise HTTPException(
                status_code=409,
                detail="A custom commission cannot reduce the rate in accepted terms",
            )
        partner.custom_commission_rate_bps = custom_rate
    if "custom_commission_months" in updates:
        custom_months = updates["custom_commission_months"]
        if custom_months is not None and custom_months < assigned_terms.commission_months:
            raise HTTPException(
                status_code=409,
                detail="A custom duration cannot reduce the period in accepted terms",
            )
        partner.custom_commission_months = custom_months
    if partner.risk_status != "clear" and len((partner.hold_reason or "").strip()) < 20:
        raise HTTPException(
            status_code=400,
            detail="A risk review or hold requires a 20-character reason",
        )
    if partner.risk_status == "clear":
        partner.hold_reason = None
    audit(
        db,
        "partner.updated",
        actor_user_id=current_user.id,
        partner_id=partner.id,
        subject_user_id=partner.user_id,
        payload={
            "fields": sorted(updates),
            "previous_risk_status": previous_risk_status,
            "risk_status": partner.risk_status,
            "risk_review_note": risk_review_note or None,
        },
    )
    db.commit()
    db.refresh(partner)
    return {"partner": _partner_payload(db, partner)}


@admin_router.put("/partners/{partner_id}/compliance")
def admin_update_partner_compliance(
    partner_id: int,
    body: ComplianceBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_finance_admin(db, current_user)
    partner = db.query(AffiliatePartner).filter(AffiliatePartner.id == partner_id).first()
    if not partner:
        raise HTTPException(status_code=404, detail="Partner not found")
    try:
        profile = update_partner_compliance(
            db,
            partner=partner,
            admin=current_user,
            **body.model_dump(),
        )
    except AffiliateProgramError as exc:
        _raise(exc)
    return {
        "partner": _partner_payload(db, partner),
        "compliance": compliance_payload(profile),
    }


@admin_router.get("/payouts")
def admin_list_payouts(
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    query = db.query(AffiliatePayout)
    if status:
        query = query.filter(AffiliatePayout.status == status)
    payouts = query.order_by(AffiliatePayout.created_at.desc()).limit(500).all()
    return {"payouts": [_payout_payload(payout) for payout in payouts]}


@admin_router.get("/payouts/{payout_id}/statement.csv")
def admin_export_affiliate_payout_statement(
    payout_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    payout = db.query(AffiliatePayout).filter(AffiliatePayout.id == payout_id).first()
    if not payout:
        raise HTTPException(status_code=404, detail="Payout statement not found")
    return _csv_response(
        _payout_statement_rows(db, payout),
        f"affiliate-payout-{payout.id}.csv",
    )


@admin_router.get("/reconciliation")
def admin_affiliate_reconciliation(
    include_stripe: bool = Query(default=False),
    invoice_limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    if not include_stripe:
        return database_reconciliation_report(db)
    _require_finance_admin(db, current_user)
    if stripe is None or not os.getenv("STRIPE_SECRET_KEY"):
        raise HTTPException(status_code=503, detail="Stripe is not configured")
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
    return stripe_reconciliation_report(db, stripe, invoice_limit=invoice_limit)


@admin_router.post("/reconciliation/invoice")
def admin_reconcile_paid_invoice(
    body: ReconcileInvoiceBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Preview or idempotently backfill one Stripe paid-invoice decision."""
    _require_finance_admin(db, current_user)
    if stripe is None or not os.getenv("STRIPE_SECRET_KEY"):
        raise HTTPException(status_code=503, detail="Stripe is not configured")
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
    try:
        invoice = stripe.Invoice.retrieve(body.invoice_id)
        invoice = invoice_with_complete_lines(invoice, stripe)
    except Exception:
        logger.exception("Affiliate invoice reconciliation retrieval failed invoice=%s", body.invoice_id)
        raise HTTPException(status_code=502, detail="Could not retrieve the Stripe invoice") from None
    if str(stripe_field(invoice, "status") or "").lower() != "paid":
        raise HTTPException(status_code=409, detail="Only a paid Stripe invoice can be reconciled")
    user = user_for_invoice(db, invoice)
    if not user:
        raise HTTPException(status_code=404, detail="No Editube account matches this invoice")
    existing_entry = (
        db.query(AffiliateCommissionEntry)
        .filter(AffiliateCommissionEntry.source_key == f"invoice:{body.invoice_id}:accrual")
        .first()
    )
    existing_state = (
        db.query(AffiliateCommissionState)
        .filter(AffiliateCommissionState.stripe_invoice_id == body.invoice_id)
        .first()
    )
    has_active_attribution = user_has_active_affiliate_attribution(db, user.id)
    has_exclusion_decision = (
        affiliate_invoice_decision_exists(db, body.invoice_id)
        and existing_entry is None
        and existing_state is None
    )
    eligible_for_backfill = bool(
        has_active_attribution
        and existing_entry is None
        and existing_state is None
        and not has_exclusion_decision
    )
    decision = (
        "existing_ledger"
        if existing_entry and existing_state
        else "inconsistent_existing_record"
        if existing_entry or existing_state
        else "recorded_exclusion"
        if has_exclusion_decision
        else "no_active_attribution"
        if not has_active_attribution
        else "ready"
    )
    preview = {
        "invoice_id": body.invoice_id,
        "user_id": user.id,
        "currency": str(stripe_field(invoice, "currency") or "").lower(),
        "amount_paid_minor": int(stripe_field(invoice, "amount_paid") or 0),
        "existing_entry_id": existing_entry.id if existing_entry else None,
        "existing_state_id": existing_state.id if existing_state else None,
        "eligible_for_backfill": eligible_for_backfill,
        "decision": decision,
        "applied": False,
        "entry": _entry_payload(existing_entry) if existing_entry else None,
    }
    if not body.apply or not eligible_for_backfill:
        return preview
    try:
        entry = None
        entry = record_paid_invoice(
            db,
            user=user,
            invoice=invoice,
            stripe_event_id=f"admin-reconcile:{body.invoice_id}",
        )
        audit(
            db,
            "reconciliation.invoice_backfill" if entry else "reconciliation.invoice_excluded",
            actor_user_id=current_user.id,
            partner_id=entry.partner_id if entry else None,
            subject_user_id=user.id,
            source_ref=body.invoice_id,
            payload={"entry_id": entry.id if entry else None},
        )
        db.commit()
    except AffiliateProgramError as exc:
        _raise(exc)
    preview["applied"] = entry is not None
    preview["entry"] = _entry_payload(entry) if entry else None
    preview["eligible_for_backfill"] = False
    preview["decision"] = "applied" if entry else "recorded_exclusion"
    return preview


@admin_router.post("/adjustments", status_code=201)
def admin_create_adjustment(
    body: ManualAdjustmentBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_finance_admin(db, current_user)
    partner = db.query(AffiliatePartner).filter(AffiliatePartner.id == body.partner_id).first()
    if not partner:
        raise HTTPException(status_code=404, detail="Partner not found")
    try:
        entry = record_manual_adjustment(
            db,
            partner=partner,
            attribution_id=body.attribution_id,
            amount_minor=body.amount_minor,
            reason=body.reason,
            idempotency_key=body.idempotency_key,
            admin=current_user,
        )
    except AffiliateProgramError as exc:
        _raise(exc)
    return {"entry": _entry_payload(entry)}


@admin_router.post("/payouts", status_code=201)
def admin_create_payout(
    body: PayoutCreateBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_finance_admin(db, current_user)
    partner = db.query(AffiliatePartner).filter(AffiliatePartner.id == body.partner_id).first()
    if not partner:
        raise HTTPException(status_code=404, detail="Partner not found")
    try:
        payout = create_payout_batch(db, partner=partner, admin=current_user, through=body.through)
    except AffiliateProgramError as exc:
        _raise(exc)
    return {"payout": _payout_payload(payout)}


@admin_router.post("/payouts/{payout_id}/approve")
def admin_approve_payout(
    payout_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_finance_admin(db, current_user)
    payout = db.query(AffiliatePayout).filter(AffiliatePayout.id == payout_id).first()
    if not payout:
        raise HTTPException(status_code=404, detail="Payout not found")
    try:
        payout = approve_payout(db, payout, current_user)
    except AffiliateProgramError as exc:
        _raise(exc)
    return {"payout": _payout_payload(payout)}


@admin_router.post("/payouts/{payout_id}/execute")
def admin_execute_payout(
    payout_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_finance_admin(db, current_user)
    payout = db.query(AffiliatePayout).filter(AffiliatePayout.id == payout_id).first()
    if not payout:
        raise HTTPException(status_code=404, detail="Payout not found")
    try:
        payout = execute_payout(db, payout, current_user)
    except AffiliateProgramError as exc:
        _raise(exc)
    return {"payout": _payout_payload(payout)}


@admin_router.get("/audit")
def admin_audit_log(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    events = db.query(AffiliateAuditEvent).order_by(AffiliateAuditEvent.created_at.desc()).limit(limit).all()
    return {
        "events": [
            {
                "id": event.id,
                "event_type": event.event_type,
                "actor_user_id": event.actor_user_id,
                "partner_id": event.partner_id,
                "subject_user_id": event.subject_user_id,
                "source_ref": event.source_ref,
                "payload": event.payload,
                "created_at": event.created_at,
            }
            for event in events
        ]
    }
