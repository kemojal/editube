"""Cash affiliate program domain service.

The affiliate program is intentionally independent from refer-a-friend. A
guest pass rewards a product user with product credits; this module governs an
application-gated commercial relationship, collected-revenue commissions, and
cash payouts.

Financial facts are append-only. A paid invoice adds a positive commission
entry. Refunds and disputes add compensating negative entries. No webhook ever
edits historical money, which makes replays, support investigations, and payout
reconciliation tractable.
"""

from __future__ import annotations

import calendar
import hashlib
import hmac
import logging
import os
import re
import secrets
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlencode, urlparse

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

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
    Referral,
    User,
    Workspace,
    WorkspaceMember,
)

logger = logging.getLogger(__name__)


PROGRAM_TERMS_TEXT = """Editube Affiliate Program Terms

Eligibility and acceptance. Participation is application-only. Approval is at
Editube's discretion. A partner may promote Editube only after accepting the
published version of these terms and may not assign or transfer their account.

Attribution. Editube uses the first eligible affiliate click recorded within the
attribution window in the versioned Commercial Summary. Self-referrals,
fabricated identities, cookie stuffing, forced redirects, misleading claims,
trademark bidding, unsolicited messages, and interference with another
partner's attribution are prohibited. Affiliate attribution does not stack with
the refer-a-friend guest-pass program; when both are presented at signup, the
guest pass takes precedence.

Commission. The applicable rate and eligibility period are stated in the
versioned Commercial Summary and run from the referred customer's first paid
invoice. Commission applies only to eligible subscription cash actually
collected, after discounts and excluding taxes, refunds, disputes, chargebacks,
credits, and non-subscription charges. Editube's signed ledger controls where
third-party reports differ.

Payout. The payout currency, hold period, and minimum payable balance are stated
in the versioned Commercial Summary. Identity, tax, sanctions, and Stripe
Connect verification must be complete. Negative adjustments carry forward and
may be offset against later commissions.

Changes and termination. New commercial terms require a new version and fresh
acceptance where they materially affect an existing partner. Editube may hold
or suspend activity while investigating abuse, and may close participation for
breach. Earned, non-fraudulent balances remain subject to refunds, disputes,
verification, and applicable law.

Content and law. Partners must use accurate, lawful disclosures and approved
brand assets. They are independent contractors and have no authority to bind
Editube. Confidential information and customer personal data must be protected.
The program is unavailable where prohibited by law.
"""

PROGRAM_TERMS_CHECKSUM = hashlib.sha256(PROGRAM_TERMS_TEXT.encode("utf-8")).hexdigest()
AFFILIATE_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
AFFILIATE_CODE_LENGTH = 10
SUPPORTED_CHANNELS = {
    "blog",
    "community",
    "email",
    "newsletter",
    "podcast",
    "social",
    "video",
    "other",
}
REQUIRED_LAUNCH_APPROVAL_ROLES = ("legal", "finance", "product", "engineering")
COMMON_PERSONAL_EMAIL_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "icloud.com",
    "me.com",
    "yahoo.com",
    "proton.me",
    "protonmail.com",
}
CAMPAIGN_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,48}[a-z0-9])?$")


def supported_countries() -> set[str]:
    """Countries explicitly approved for this platform's payout corridor.

    Stripe account availability is broader than cross-border transfer
    availability. There is deliberately no code default: legal approval alone
    must never open applications in countries the platform cannot pay.
    """
    return {
        country
        for country in (
            item.strip().upper()
            for item in os.getenv("AFFILIATE_SUPPORTED_COUNTRIES", "").split(",")
        )
        if len(country) == 2 and country.isalpha()
    }


class AffiliateProgramError(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def legal_launch_ready() -> bool:
    return _truthy_env("AFFILIATE_LEGAL_APPROVED")


def launch_approval_state(
    db: Session,
    terms: AffiliateProgramTerms | None,
) -> dict:
    approvals = []
    if terms is not None:
        approvals = (
            db.query(AffiliateLaunchApproval)
            .filter(
                AffiliateLaunchApproval.terms_version_id == terms.id,
                AffiliateLaunchApproval.revoked_at.is_(None),
            )
            .order_by(AffiliateLaunchApproval.approval_role.asc())
            .all()
        )
    valid = {
        item.approval_role: item
        for item in approvals
        if terms is not None and item.terms_checksum == terms.legal_copy_checksum
    }
    missing = [role for role in REQUIRED_LAUNCH_APPROVAL_ROLES if role not in valid]
    distinct_approvers = {item.approved_by_user_id for item in valid.values()}
    distinct_required = not _truthy_env("AFFILIATE_ALLOW_MULTIROLE_APPROVER")
    separation_ok = not distinct_required or len(distinct_approvers) == len(valid)
    return {
        "ready": not missing and separation_ok,
        "missing_roles": missing,
        "separation_of_duties": separation_ok,
        "approvals": [
            {
                "id": item.id,
                "role": item.approval_role,
                "approved_by_user_id": item.approved_by_user_id,
                "approved_at": item.approved_at,
                "note": item.note,
            }
            for item in approvals
        ],
    }


def terms_launch_ready(db: Session, terms: AffiliateProgramTerms | None) -> bool:
    return bool(
        terms
        and terms.status == "active"
        and terms.legal_text
        and hashlib.sha256(terms.legal_text.encode("utf-8")).hexdigest()
        == terms.legal_copy_checksum
        and terms.legal_copy_checksum == PROGRAM_TERMS_CHECKSUM
        and legal_launch_ready()
        and launch_approval_state(db, terms)["ready"]
    )


def payouts_runtime_enabled() -> bool:
    return _truthy_env("AFFILIATE_PAYOUTS_ENABLED")


def _hash_secret() -> bytes:
    secret = (
        os.getenv("AFFILIATE_HASH_SECRET")
        or os.getenv("SECRET_KEY")
        or os.getenv("JWT_SECRET_KEY")
        or "local-affiliate-hash-secret"
    )
    return secret.encode("utf-8")


def privacy_hash(value: str | None) -> str | None:
    normalized = (value or "").strip()
    if not normalized:
        return None
    return hmac.new(_hash_secret(), normalized.encode("utf-8"), hashlib.sha256).hexdigest()


def audit(
    db: Session,
    event_type: str,
    *,
    actor_user_id: int | None = None,
    partner_id: int | None = None,
    subject_user_id: int | None = None,
    source_ref: str | None = None,
    payload: dict | None = None,
) -> AffiliateAuditEvent:
    event = AffiliateAuditEvent(
        event_type=event_type,
        actor_user_id=actor_user_id,
        partner_id=partner_id,
        subject_user_id=subject_user_id,
        source_ref=source_ref,
        payload=payload,
    )
    db.add(event)
    return event


def active_terms(db: Session) -> AffiliateProgramTerms | None:
    now = utcnow()
    return (
        db.query(AffiliateProgramTerms)
        .filter(
            AffiliateProgramTerms.status == "active",
            AffiliateProgramTerms.effective_at.isnot(None),
            AffiliateProgramTerms.effective_at <= now,
        )
        .order_by(AffiliateProgramTerms.effective_at.desc(), AffiliateProgramTerms.id.desc())
        .first()
    )


def latest_terms(db: Session) -> AffiliateProgramTerms | None:
    return (
        db.query(AffiliateProgramTerms)
        .order_by(AffiliateProgramTerms.created_at.desc(), AffiliateProgramTerms.id.desc())
        .first()
    )


def terms_payload(terms: AffiliateProgramTerms | None, db: Session | None = None) -> dict:
    if terms is None:
        return {
            "version": "v1",
            "status": "unavailable",
            "commission_rate_bps": 3000,
            "commission_months": 12,
            "attribution_window_days": 60,
            "payout_minimum_minor": 5000,
            "hold_days": 30,
            "currency": "usd",
            "commission_basis": "invoice_amount_paid_excluding_tax",
            "legal_copy_checksum": PROGRAM_TERMS_CHECKSUM,
            "legal_text": PROGRAM_TERMS_TEXT,
            "effective_at": None,
            "accepting_applications": False,
        }
    return {
        "id": terms.id,
        "version": terms.version,
        "status": terms.status,
        "commission_rate_bps": terms.commission_rate_bps,
        "commission_months": terms.commission_months,
        "attribution_window_days": terms.attribution_window_days,
        "payout_minimum_minor": terms.payout_minimum_minor,
        "hold_days": terms.hold_days,
        "currency": terms.currency,
        "commission_basis": terms.commission_basis,
        "legal_copy_checksum": terms.legal_copy_checksum,
        "legal_text": terms.legal_text,
        "effective_at": terms.effective_at,
        "accepting_applications": bool(
            db
            and terms_launch_ready(db, terms)
            and supported_countries()
        ),
    }


def record_launch_approval(
    db: Session,
    *,
    terms: AffiliateProgramTerms,
    role: str,
    admin: User,
    note: str,
) -> AffiliateLaunchApproval:
    normalized_role = (role or "").strip().lower()
    if normalized_role not in REQUIRED_LAUNCH_APPROVAL_ROLES:
        raise AffiliateProgramError("invalid_role", "Approval role is not recognized.")
    if terms.status not in {"draft", "active"}:
        raise AffiliateProgramError(
            "invalid_state",
            "Only draft or active terms can receive launch approvals.",
        )
    if hashlib.sha256(terms.legal_text.encode("utf-8")).hexdigest() != terms.legal_copy_checksum:
        raise AffiliateProgramError("copy_mismatch", "Terms checksum verification failed.")
    clean_note = (note or "").strip()
    if len(clean_note) < 20:
        raise AffiliateProgramError("notes_required", "Record at least 20 characters of approval evidence.")
    existing = (
        db.query(AffiliateLaunchApproval)
        .filter(
            AffiliateLaunchApproval.terms_version_id == terms.id,
            AffiliateLaunchApproval.approval_role == normalized_role,
            AffiliateLaunchApproval.revoked_at.is_(None),
        )
        .first()
    )
    if existing:
        if existing.approved_by_user_id == admin.id:
            return existing
        raise AffiliateProgramError(
            "approval_exists",
            "That launch role was already approved by another administrator.",
        )
    if not _truthy_env("AFFILIATE_ALLOW_MULTIROLE_APPROVER"):
        duplicate_approver = (
            db.query(AffiliateLaunchApproval.id)
            .filter(
                AffiliateLaunchApproval.terms_version_id == terms.id,
                AffiliateLaunchApproval.approved_by_user_id == admin.id,
                AffiliateLaunchApproval.revoked_at.is_(None),
            )
            .first()
        )
        if duplicate_approver:
            raise AffiliateProgramError(
                "separation_required",
                "A different verified administrator must approve each launch role.",
            )
    approval = AffiliateLaunchApproval(
        terms_version_id=terms.id,
        approval_role=normalized_role,
        approved_by_user_id=admin.id,
        terms_checksum=terms.legal_copy_checksum,
        note=clean_note[:4000],
    )
    db.add(approval)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        concurrent = (
            db.query(AffiliateLaunchApproval)
            .filter(
                AffiliateLaunchApproval.terms_version_id == terms.id,
                AffiliateLaunchApproval.approval_role == normalized_role,
                AffiliateLaunchApproval.revoked_at.is_(None),
            )
            .first()
        )
        if concurrent and concurrent.approved_by_user_id == admin.id:
            return concurrent
        raise AffiliateProgramError(
            "approval_exists",
            "That launch role was already approved by another administrator.",
        ) from None
    audit(
        db,
        "launch.approved",
        actor_user_id=admin.id,
        source_ref=f"terms:{terms.id}:{normalized_role}",
        payload={"approval_id": approval.id, "role": normalized_role},
    )
    db.commit()
    db.refresh(approval)
    return approval


def revoke_launch_approval(
    db: Session,
    *,
    approval: AffiliateLaunchApproval,
    admin: User,
) -> AffiliateLaunchApproval:
    approval = (
        db.query(AffiliateLaunchApproval)
        .filter(AffiliateLaunchApproval.id == approval.id)
        .with_for_update()
        .first()
    )
    if not approval:
        raise AffiliateProgramError("not_found", "Launch approval not found.")
    if approval.revoked_at is None:
        approval.revoked_at = utcnow()
        approval.revoked_by_user_id = admin.id
        audit(
            db,
            "launch.approval_revoked",
            actor_user_id=admin.id,
            source_ref=f"launch-approval:{approval.id}",
            payload={"role": approval.approval_role},
        )
        db.commit()
        db.refresh(approval)
    return approval


def publish_terms(db: Session, terms: AffiliateProgramTerms, admin: User) -> AffiliateProgramTerms:
    if not legal_launch_ready():
        raise AffiliateProgramError(
            "legal_not_approved",
            "Set AFFILIATE_LEGAL_APPROVED only after the program terms have passed legal review.",
        )
    if terms.status != "draft":
        raise AffiliateProgramError("invalid_state", "Only draft terms can be published.")
    stored_checksum = hashlib.sha256(terms.legal_text.encode("utf-8")).hexdigest()
    if (
        terms.legal_copy_checksum != PROGRAM_TERMS_CHECKSUM
        or stored_checksum != terms.legal_copy_checksum
    ):
        raise AffiliateProgramError(
            "copy_mismatch",
            "The stored terms checksum does not match the legal text deployed by this release.",
        )
    approval_state = launch_approval_state(db, terms)
    if not approval_state["ready"]:
        detail = ", ".join(approval_state["missing_roles"]) or "separation of duties"
        raise AffiliateProgramError(
            "launch_approvals_missing",
            f"Required launch approvals are incomplete: {detail}.",
        )
    now = utcnow()
    for previous in (
        db.query(AffiliateProgramTerms)
        .filter(AffiliateProgramTerms.status == "active")
        .with_for_update()
        .all()
    ):
        previous.status = "retired"
        previous.retired_at = now
    terms.status = "active"
    terms.effective_at = now
    audit(
        db,
        "terms.published",
        actor_user_id=admin.id,
        source_ref=f"terms:{terms.id}",
        payload={"version": terms.version, "checksum": terms.legal_copy_checksum},
    )
    db.commit()
    db.refresh(terms)
    return terms


def create_terms_draft(
    db: Session,
    *,
    admin: User,
    version: str,
    commission_rate_bps: int,
    commission_months: int,
    attribution_window_days: int,
    payout_minimum_minor: int,
    hold_days: int,
) -> AffiliateProgramTerms:
    if db.query(AffiliateProgramTerms).filter(AffiliateProgramTerms.version == version).first():
        raise AffiliateProgramError("duplicate_version", "That terms version already exists.")
    if not 0 <= commission_rate_bps <= 10_000:
        raise AffiliateProgramError("invalid_rate", "Commission rate must be between 0% and 100%.")
    if not 1 <= commission_months <= 60:
        raise AffiliateProgramError("invalid_months", "Commission duration must be 1 to 60 months.")
    if not 1 <= attribution_window_days <= 365:
        raise AffiliateProgramError("invalid_window", "Attribution window must be 1 to 365 days.")
    if not 0 <= hold_days <= 180:
        raise AffiliateProgramError("invalid_hold", "Payout hold must be 0 to 180 days.")
    if payout_minimum_minor < 1:
        raise AffiliateProgramError("invalid_threshold", "Payout threshold must be positive.")
    terms = AffiliateProgramTerms(
        version=version.strip(),
        status="draft",
        commission_rate_bps=commission_rate_bps,
        commission_months=commission_months,
        attribution_window_days=attribution_window_days,
        payout_minimum_minor=payout_minimum_minor,
        hold_days=hold_days,
        currency="usd",
        commission_basis="invoice_amount_paid_excluding_tax",
        legal_text=PROGRAM_TERMS_TEXT,
        legal_copy_checksum=PROGRAM_TERMS_CHECKSUM,
        created_by_user_id=admin.id,
    )
    db.add(terms)
    db.flush()
    audit(
        db,
        "terms.draft_created",
        actor_user_id=admin.id,
        source_ref=f"terms:{terms.id}",
        payload={"version": terms.version},
    )
    db.commit()
    db.refresh(terms)
    return terms


def _generate_partner_code() -> str:
    return "EDT" + "".join(
        secrets.choice(AFFILIATE_CODE_ALPHABET) for _ in range(AFFILIATE_CODE_LENGTH)
    )


def normalize_channels(channels: Iterable[str]) -> list[str]:
    result: list[str] = []
    for raw in channels:
        channel = (raw or "").strip().lower()
        if channel not in SUPPORTED_CHANNELS:
            raise AffiliateProgramError("invalid_channel", f"Unsupported channel: {raw}")
        if channel not in result:
            result.append(channel)
    if not result:
        raise AffiliateProgramError("missing_channels", "Select at least one promotion channel.")
    return result


def normalize_website(value: str | None) -> str | None:
    website = (value or "").strip()
    if not website:
        return None
    parsed = urlparse(website)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AffiliateProgramError("invalid_website", "Website must be a complete HTTP or HTTPS URL.")
    return website[:500]


def submit_application(
    db: Session,
    *,
    user: User,
    display_name: str,
    business_name: str | None,
    website_url: str | None,
    country_code: str,
    audience_description: str,
    audience_size: int | None,
    promotion_channels: Iterable[str],
    attested: bool,
) -> AffiliateApplication:
    terms = active_terms(db)
    if not terms_launch_ready(db, terms):
        raise AffiliateProgramError(
            "applications_closed", "Affiliate applications are not open yet."
        )
    if db.query(AffiliatePartner).filter(AffiliatePartner.user_id == user.id).first():
        raise AffiliateProgramError("already_partner", "This account already has a partner profile.")
    pending = (
        db.query(AffiliateApplication)
        .filter(
            AffiliateApplication.user_id == user.id,
            AffiliateApplication.status == "pending",
        )
        .first()
    )
    if pending:
        raise AffiliateProgramError("application_pending", "Your application is already under review.")
    recent_attempts = (
        db.query(func.count(AffiliateApplication.id))
        .filter(
            AffiliateApplication.user_id == user.id,
            AffiliateApplication.created_at >= utcnow() - timedelta(days=30),
        )
        .scalar()
        or 0
    )
    if recent_attempts >= 3:
        raise AffiliateProgramError(
            "application_rate_limited",
            "You have reached the application limit for this review period.",
        )
    if not attested:
        raise AffiliateProgramError("attestation_required", "Confirm the application is accurate.")
    name = (display_name or "").strip()
    description = (audience_description or "").strip()
    country = (country_code or "").strip().upper()
    if len(name) < 2 or len(name) > 120:
        raise AffiliateProgramError("invalid_name", "Display name must be 2 to 120 characters.")
    if len(description) < 80 or len(description) > 2000:
        raise AffiliateProgramError(
            "invalid_description", "Audience description must be 80 to 2,000 characters."
        )
    if country not in supported_countries():
        raise AffiliateProgramError(
            "unsupported_country",
            "The initial affiliate launch is not available in that country.",
        )
    if audience_size is not None and not 0 <= audience_size <= 2_000_000_000:
        raise AffiliateProgramError("invalid_audience_size", "Audience size cannot be negative.")

    application = AffiliateApplication(
        user_id=user.id,
        email=user.email,
        display_name=name,
        business_name=(business_name or "").strip()[:160] or None,
        website_url=normalize_website(website_url),
        country_code=country,
        audience_description=description,
        audience_size=audience_size,
        promotion_channels=normalize_channels(promotion_channels),
        payout_currency="usd",
        status="pending",
        applicant_attested_at=utcnow(),
    )
    try:
        db.add(application)
        db.flush()
        audit(
            db,
            "application.submitted",
            actor_user_id=user.id,
            subject_user_id=user.id,
            source_ref=f"application:{application.id}",
            payload={"country_code": country, "channels": application.promotion_channels},
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        concurrent = (
            db.query(AffiliateApplication)
            .filter(
                AffiliateApplication.user_id == user.id,
                AffiliateApplication.status == "pending",
            )
            .first()
        )
        if concurrent:
            raise AffiliateProgramError(
                "application_pending", "Your application is already under review."
            ) from None
        raise
    db.refresh(application)
    return application


def withdraw_application(
    db: Session,
    *,
    application_id: int,
    user: User,
) -> AffiliateApplication:
    application = (
        db.query(AffiliateApplication)
        .filter(
            AffiliateApplication.id == application_id,
            AffiliateApplication.user_id == user.id,
        )
        .with_for_update()
        .first()
    )
    if not application:
        raise AffiliateProgramError("not_found", "Application not found.")
    if application.status != "pending":
        raise AffiliateProgramError(
            "invalid_state", "Only a pending application can be withdrawn."
        )
    application.status = "withdrawn"
    application.reviewed_at = utcnow()
    audit(
        db,
        "application.withdrawn",
        actor_user_id=user.id,
        subject_user_id=user.id,
        source_ref=f"application:{application.id}",
    )
    db.commit()
    db.refresh(application)
    return application


def review_application(
    db: Session,
    *,
    application: AffiliateApplication,
    admin: User,
    decision: str,
    notes: str | None,
) -> tuple[AffiliateApplication, AffiliatePartner | None]:
    application = (
        db.query(AffiliateApplication)
        .filter(AffiliateApplication.id == application.id)
        .with_for_update()
        .first()
    )
    if application is None:
        raise AffiliateProgramError("not_found", "Application not found.")
    if application.status != "pending":
        raise AffiliateProgramError("already_reviewed", "This application has already been reviewed.")
    decision = decision.strip().lower()
    if decision not in {"approved", "rejected"}:
        raise AffiliateProgramError("invalid_decision", "Decision must be approved or rejected.")
    if decision == "rejected" and len((notes or "").strip()) < 10:
        raise AffiliateProgramError("notes_required", "Record a clear rejection reason.")

    now = utcnow()
    application.status = decision
    application.reviewed_by_user_id = admin.id
    application.review_notes = (notes or "").strip()[:4000] or None
    application.reviewed_at = now
    partner: AffiliatePartner | None = None

    if decision == "approved":
        terms = active_terms(db)
        if not terms_launch_ready(db, terms):
            raise AffiliateProgramError(
                "terms_unavailable", "Publish legally approved terms before approving partners."
            )
        if application.country_code not in supported_countries():
            raise AffiliateProgramError(
                "unsupported_country",
                "This application country is not enabled for the platform's payout corridor.",
            )
        existing = (
            db.query(AffiliatePartner)
            .filter(AffiliatePartner.user_id == application.user_id)
            .first()
        )
        if existing:
            raise AffiliateProgramError("already_partner", "This user is already a partner.")
        for _ in range(8):
            candidate = _generate_partner_code()
            try:
                with db.begin_nested():
                    partner = AffiliatePartner(
                        user_id=application.user_id,
                        application_id=application.id,
                        terms_version_id=terms.id,
                        code=candidate,
                        status="pending_terms",
                        approved_at=now,
                        payouts_enabled=False,
                        risk_status="review",
                        hold_reason="Compliance review pending.",
                    )
                    db.add(partner)
                    db.flush()
                break
            except IntegrityError:
                partner = None
        if partner is None:
            raise AffiliateProgramError("code_allocation_failed", "Could not allocate a partner code.")

    audit(
        db,
        f"application.{decision}",
        actor_user_id=admin.id,
        partner_id=partner.id if partner else None,
        subject_user_id=application.user_id,
        source_ref=f"application:{application.id}",
        payload={"notes_present": bool(application.review_notes)},
    )
    db.commit()
    db.refresh(application)
    if partner:
        db.refresh(partner)
    return application, partner


def accept_partner_terms(
    db: Session,
    *,
    partner: AffiliatePartner,
    user: User,
    version: str,
    checksum: str,
    ip: str | None,
    user_agent: str | None,
) -> AffiliateTermsAcceptance:
    partner = (
        db.query(AffiliatePartner)
        .filter(AffiliatePartner.id == partner.id)
        .with_for_update()
        .first()
    )
    if not partner or partner.user_id != user.id:
        raise AffiliateProgramError("not_found", "Partner profile not found.")
    terms = db.query(AffiliateProgramTerms).filter(AffiliateProgramTerms.id == partner.terms_version_id).first()
    if not terms_launch_ready(db, terms):
        raise AffiliateProgramError("terms_unavailable", "These terms are not open for acceptance.")
    if version != terms.version or checksum != terms.legal_copy_checksum:
        raise AffiliateProgramError("terms_changed", "The terms changed. Review the current version and try again.")
    if checksum != PROGRAM_TERMS_CHECKSUM:
        raise AffiliateProgramError("copy_mismatch", "The displayed legal copy does not match this release.")
    existing = (
        db.query(AffiliateTermsAcceptance)
        .filter(
            AffiliateTermsAcceptance.partner_id == partner.id,
            AffiliateTermsAcceptance.terms_version_id == terms.id,
        )
        .first()
    )
    if existing:
        if partner.status == "pending_terms":
            partner.status = "active"
            db.commit()
        return existing
    acceptance = AffiliateTermsAcceptance(
        partner_id=partner.id,
        terms_version_id=terms.id,
        accepted_by_user_id=user.id,
        ip_hash=privacy_hash(ip),
        user_agent_hash=privacy_hash(user_agent),
    )
    db.add(acceptance)
    if partner.status == "pending_terms":
        partner.status = "active"
    audit(
        db,
        "terms.accepted",
        actor_user_id=user.id,
        partner_id=partner.id,
        subject_user_id=user.id,
        source_ref=f"terms:{terms.id}",
        payload={"version": terms.version, "checksum": checksum},
    )
    db.commit()
    db.refresh(acceptance)
    return acceptance


def partner_for_user(db: Session, user_id: int) -> AffiliatePartner | None:
    return db.query(AffiliatePartner).filter(AffiliatePartner.user_id == user_id).first()


def build_partner_link(
    code: str,
    campaign: str | None = None,
    destination_path: str = "/signup",
) -> str:
    base = os.getenv("FRONTEND_BASE_URL", "http://localhost:3000").rstrip("/")
    path = _clean_path(destination_path) or "/signup"
    query = {"aff": code}
    if campaign:
        query["campaign"] = campaign
    return f"{base}{path}?{urlencode(query)}"


def _clean_path(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    if (
        not raw.startswith("/")
        or raw.startswith("//")
        or "\\" in raw
        or any(character.isspace() or ord(character) < 32 for character in raw)
    ):
        return None
    return raw[:500]


def normalize_campaign_slug(value: str) -> str:
    slug = (value or "").strip().lower()
    if not CAMPAIGN_SLUG_RE.fullmatch(slug):
        raise AffiliateProgramError(
            "invalid_campaign",
            "Campaign slugs must be 1–50 lowercase letters, numbers, hyphens, or underscores.",
        )
    return slug


def campaign_payload(campaign: AffiliateCampaign, partner_code: str) -> dict:
    return {
        "id": campaign.id,
        "slug": campaign.slug,
        "name": campaign.name,
        "destination_path": campaign.destination_path,
        "status": campaign.status,
        "link": build_partner_link(
            partner_code,
            campaign.slug,
            campaign.destination_path,
        ),
        "created_at": campaign.created_at,
        "updated_at": campaign.updated_at,
    }


def list_partner_campaigns(db: Session, partner: AffiliatePartner) -> list[AffiliateCampaign]:
    return (
        db.query(AffiliateCampaign)
        .filter(AffiliateCampaign.partner_id == partner.id)
        .order_by(AffiliateCampaign.created_at.desc(), AffiliateCampaign.id.desc())
        .all()
    )


def create_partner_campaign(
    db: Session,
    *,
    partner: AffiliatePartner,
    name: str,
    slug: str,
    destination_path: str,
) -> AffiliateCampaign:
    if partner.status != "active" or partner.risk_status != "clear":
        raise AffiliateProgramError("partner_held", "Resolve the partner hold before creating links.")
    clean_name = (name or "").strip()
    if not 2 <= len(clean_name) <= 120:
        raise AffiliateProgramError("invalid_campaign", "Campaign name must be 2–120 characters.")
    clean_slug = normalize_campaign_slug(slug)
    clean_path = _clean_path(destination_path)
    if not clean_path or "?" in clean_path or "#" in clean_path:
        raise AffiliateProgramError("invalid_campaign", "Campaign destination must be an internal path.")
    active_count = (
        db.query(func.count(AffiliateCampaign.id))
        .filter(
            AffiliateCampaign.partner_id == partner.id,
            AffiliateCampaign.status != "archived",
        )
        .scalar()
        or 0
    )
    if active_count >= 100:
        raise AffiliateProgramError("campaign_limit", "Archive an existing campaign before creating another.")
    campaign = AffiliateCampaign(
        partner_id=partner.id,
        name=clean_name,
        slug=clean_slug,
        destination_path=clean_path,
        status="active",
    )
    db.add(campaign)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise AffiliateProgramError("duplicate_campaign", "That campaign slug is already in use.") from None
    audit(
        db,
        "campaign.created",
        actor_user_id=partner.user_id,
        partner_id=partner.id,
        source_ref=f"campaign:{campaign.id}",
        payload={"slug": campaign.slug, "destination_path": campaign.destination_path},
    )
    db.commit()
    db.refresh(campaign)
    return campaign


def update_partner_campaign(
    db: Session,
    *,
    partner: AffiliatePartner,
    campaign: AffiliateCampaign,
    name: str | None = None,
    destination_path: str | None = None,
    status: str | None = None,
) -> AffiliateCampaign:
    campaign = (
        db.query(AffiliateCampaign)
        .filter(
            AffiliateCampaign.id == campaign.id,
            AffiliateCampaign.partner_id == partner.id,
        )
        .with_for_update()
        .first()
    )
    if not campaign:
        raise AffiliateProgramError("not_found", "Campaign not found.")
    if campaign.status == "archived" and any(
        value is not None for value in (name, destination_path, status)
    ):
        raise AffiliateProgramError("invalid_state", "Archived campaigns are immutable.")
    if name is not None:
        clean_name = name.strip()
        if not 2 <= len(clean_name) <= 120:
            raise AffiliateProgramError("invalid_campaign", "Campaign name must be 2–120 characters.")
        campaign.name = clean_name
    if destination_path is not None:
        clean_path = _clean_path(destination_path)
        if not clean_path or "?" in clean_path or "#" in clean_path:
            raise AffiliateProgramError("invalid_campaign", "Campaign destination must be an internal path.")
        campaign.destination_path = clean_path
    if status is not None:
        normalized_status = status.strip().lower()
        if normalized_status not in {"active", "paused", "archived"}:
            raise AffiliateProgramError("invalid_campaign", "Campaign status is invalid.")
        if normalized_status == "active" and (
            partner.status != "active" or partner.risk_status != "clear"
        ):
            raise AffiliateProgramError(
                "partner_held",
                "Resolve the partner hold before activating campaign links.",
            )
        campaign.status = normalized_status
    audit(
        db,
        "campaign.updated",
        actor_user_id=partner.user_id,
        partner_id=partner.id,
        source_ref=f"campaign:{campaign.id}",
        payload={"status": campaign.status, "destination_path": campaign.destination_path},
    )
    db.commit()
    db.refresh(campaign)
    return campaign


def _clean_host(value: str | None) -> str | None:
    raw = (value or "").strip().lower()
    if not raw:
        return None
    try:
        return (urlparse(raw).hostname or raw.split("/")[0])[:255]
    except ValueError:
        return None


def record_click(
    db: Session,
    *,
    code: str,
    campaign: str | None,
    landing_path: str | None,
    referrer: str | None,
    ip: str | None,
    user_agent: str | None,
) -> AffiliateClick:
    normalized = (code or "").strip().upper()
    partner = (
        db.query(AffiliatePartner)
        .filter(AffiliatePartner.code == normalized, AffiliatePartner.status == "active")
        .first()
    )
    if not partner:
        raise AffiliateProgramError("invalid_link", "That affiliate link is not active.")
    terms = db.query(AffiliateProgramTerms).filter(AffiliateProgramTerms.id == partner.terms_version_id).first()
    if not terms_launch_ready(db, terms):
        raise AffiliateProgramError("terms_unavailable", "Affiliate terms are unavailable.")
    campaign_record = None
    campaign_slug = (campaign or "").strip().lower() or None
    if campaign_slug:
        campaign_slug = normalize_campaign_slug(campaign_slug)
        campaign_record = (
            db.query(AffiliateCampaign)
            .filter(
                AffiliateCampaign.partner_id == partner.id,
                AffiliateCampaign.slug == campaign_slug,
                AffiliateCampaign.status == "active",
            )
            .first()
        )
        if not campaign_record:
            raise AffiliateProgramError("invalid_campaign", "That campaign link is not active.")
    now = utcnow()
    ip_digest = privacy_hash(ip)
    risk_flags: list[str] = []
    if ip_digest:
        recent = (
            db.query(func.count(AffiliateClick.id))
            .filter(
                AffiliateClick.partner_id == partner.id,
                AffiliateClick.ip_hash == ip_digest,
                AffiliateClick.occurred_at >= now - timedelta(hours=1),
            )
            .scalar()
            or 0
        )
        if recent >= 20:
            risk_flags.append("ip_velocity")
        if recent >= 100:
            raise AffiliateProgramError(
                "click_rate_limited", "This affiliate link is receiving too many requests."
            )
    token = secrets.token_urlsafe(32)
    click = AffiliateClick(
        partner_id=partner.id,
        token=token,
        campaign_id=campaign_record.id if campaign_record else None,
        campaign=campaign_record.slug if campaign_record else None,
        landing_path=_clean_path(landing_path),
        referrer_host=_clean_host(referrer),
        ip_hash=ip_digest,
        user_agent_hash=privacy_hash(user_agent),
        risk_flags=risk_flags or None,
        occurred_at=now,
        expires_at=now + timedelta(days=terms.attribution_window_days),
    )
    db.add(click)
    db.flush()
    audit(
        db,
        "click.recorded",
        partner_id=partner.id,
        source_ref=f"click:{click.id}",
        payload={"campaign": click.campaign, "risk_flags": risk_flags},
    )
    db.commit()
    db.refresh(click)
    return click


def _email_domain(email: str | None) -> str | None:
    normalized = (email or "").strip().lower()
    if "@" not in normalized:
        return None
    domain = normalized.rsplit("@", 1)[1]
    return domain if domain and domain not in COMMON_PERSONAL_EMAIL_DOMAINS else None


def _workspace_ids_for_user(db: Session, user_id: int) -> set[int]:
    owned = {
        row[0]
        for row in db.query(Workspace.id)
        .filter(Workspace.owner_user_id == user_id)
        .all()
    }
    memberships = {
        row[0]
        for row in db.query(WorkspaceMember.workspace_id)
        .filter(WorkspaceMember.user_id == user_id)
        .all()
    }
    return owned | memberships


def attribution_risk_flags(
    db: Session,
    *,
    partner: AffiliatePartner,
    invitee: User,
    click: AffiliateClick,
) -> list[str]:
    flags: list[str] = []
    partner_user = db.query(User).filter(User.id == partner.user_id).first()
    if not partner_user:
        return ["partner_identity_missing"]
    partner_domain = _email_domain(partner_user.email)
    invitee_domain = _email_domain(invitee.email)
    if partner_domain and partner_domain == invitee_domain:
        flags.append("shared_business_email_domain")
    if (
        partner_user.stripe_customer_id
        and invitee.stripe_customer_id
        and partner_user.stripe_customer_id == invitee.stripe_customer_id
    ):
        flags.append("shared_stripe_customer")
    if _workspace_ids_for_user(db, partner_user.id) & _workspace_ids_for_user(db, invitee.id):
        flags.append("shared_workspace")
    if click.ip_hash:
        matching_acceptance = (
            db.query(AffiliateTermsAcceptance.id)
            .filter(
                AffiliateTermsAcceptance.partner_id == partner.id,
                AffiliateTermsAcceptance.ip_hash == click.ip_hash,
            )
            .first()
        )
        if matching_acceptance:
            flags.append("shared_partner_network")
    return flags


def claim_attribution(
    db: Session,
    *,
    user: User,
    click_token: str,
) -> AffiliateAttribution:
    # Serialize all acquisition-program claims for this account. The email
    # signup path is sequential, but OAuth callbacks and direct API requests
    # can otherwise race a guest pass against an affiliate claim.
    locked_user = (
        db.query(User).filter(User.id == user.id).with_for_update().first()
    )
    if not locked_user:
        raise AffiliateProgramError("not_found", "Account not found.")
    existing = (
        db.query(AffiliateAttribution)
        .filter(AffiliateAttribution.invitee_user_id == user.id)
        .first()
    )
    if existing:
        return existing
    if db.query(Referral.id).filter(Referral.invitee_user_id == user.id).first():
        raise AffiliateProgramError(
            "acquisition_conflict",
            "This account already used a refer-a-friend guest pass.",
        )
    click = (
        db.query(AffiliateClick)
        .filter(AffiliateClick.token == (click_token or "").strip())
        .with_for_update()
        .first()
    )
    if not click or click.expires_at <= utcnow():
        raise AffiliateProgramError("click_expired", "That affiliate attribution has expired.")
    partner = (
        db.query(AffiliatePartner)
        .filter(AffiliatePartner.id == click.partner_id)
        .with_for_update()
        .first()
    )
    if not partner or partner.status != "active":
        raise AffiliateProgramError("partner_inactive", "That affiliate partner is not active.")
    if partner.user_id == user.id:
        raise AffiliateProgramError("self_referral", "Affiliate links cannot be used on your own account.")
    risk_flags = attribution_risk_flags(
        db,
        partner=partner,
        invitee=locked_user,
        click=click,
    )
    attribution = AffiliateAttribution(
        partner_id=partner.id,
        click_id=click.id,
        invitee_user_id=user.id,
        terms_version_id=partner.terms_version_id,
        status="active",
        risk_flags=risk_flags or None,
        attributed_at=utcnow(),
    )
    db.add(attribution)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        concurrent = (
            db.query(AffiliateAttribution)
            .filter(AffiliateAttribution.invitee_user_id == user.id)
            .first()
        )
        if concurrent:
            return concurrent
        raise
    audit(
        db,
        "attribution.claimed",
        partner_id=partner.id,
        subject_user_id=user.id,
        source_ref=f"attribution:{attribution.id}",
        payload={
            "click_id": click.id,
            "campaign": click.campaign,
            "risk_flags": risk_flags,
        },
    )
    if risk_flags:
        partner.risk_status = "review"
        partner.hold_reason = "A referred account matched partner-controlled identity signals."
        audit(
            db,
            "attribution.risk_review_required",
            partner_id=partner.id,
            subject_user_id=user.id,
            source_ref=f"attribution:{attribution.id}",
            payload={"risk_flags": risk_flags},
        )
    db.commit()
    db.refresh(attribution)
    return attribution


def _field(obj, key: str, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _stripe_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    return datetime.fromtimestamp(_int(value), tz=timezone.utc).replace(tzinfo=None)


def _add_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _availability_date(paid_at: datetime, hold_days: int) -> datetime:
    last_day = calendar.monthrange(paid_at.year, paid_at.month)[1]
    month_end = paid_at.replace(day=last_day, hour=23, minute=59, second=59, microsecond=0)
    return month_end + timedelta(days=hold_days)


def _round_rate(amount_minor: int, rate_bps: int) -> int:
    return int(
        (Decimal(amount_minor) * Decimal(rate_bps) / Decimal(10_000)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def invoice_commissionable_minor(invoice) -> int:
    """Subscription cash collected after discounts, tax, and account credits.

    Stripe invoices can mix recurring subscription lines with one-time charges.
    Paying commission on the invoice subtotal would therefore violate the
    program terms. Cash is allocated proportionally across the fully expanded
    invoice total; incomplete line pages fail closed for manual reconciliation.
    """
    amount_paid = max(0, _int(_field(invoice, "amount_paid")))
    if amount_paid <= 0:
        return 0
    lines = _field(invoice, "lines")
    if lines is None or bool(_field(lines, "has_more", False)):
        return 0
    line_items = _field(lines, "data", []) or []
    eligible_excluding_tax = 0
    for line in line_items:
        parent = _field(line, "parent")
        subscription_details = _field(parent, "subscription_item_details")
        is_subscription = bool(
            _field(line, "type") == "subscription"
            or _field(line, "subscription")
            or _field(line, "subscription_item")
            or _field(parent, "type") == "subscription_item_details"
            or subscription_details
        )
        if not is_subscription:
            continue
        line_amount = _field(line, "amount_excluding_tax")
        if line_amount is None:
            line_amount = _field(line, "amount")
        eligible_excluding_tax += _int(line_amount)

    eligible_excluding_tax = max(0, eligible_excluding_tax)
    invoice_total = max(0, _int(_field(invoice, "total")))
    if invoice_total <= 0:
        invoice_total = max(
            amount_paid,
            max(0, _int(_field(invoice, "total_excluding_tax"))),
        )
    cash_ratio = min(Decimal(1), Decimal(amount_paid) / Decimal(invoice_total))
    return int(
        (Decimal(eligible_excluding_tax) * cash_ratio).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def record_paid_invoice(
    db: Session,
    *,
    user: User,
    invoice,
    stripe_event_id: str | None,
) -> AffiliateCommissionEntry | None:
    attribution = (
        db.query(AffiliateAttribution)
        .filter(
            AffiliateAttribution.invitee_user_id == user.id,
            AffiliateAttribution.status == "active",
        )
        .with_for_update()
        .first()
    )
    if not attribution:
        return None
    invoice_id = str(_field(invoice, "id") or "").strip()
    if not invoice_id:
        raise AffiliateProgramError("invalid_invoice", "Paid invoice has no Stripe id.")
    partner = db.query(AffiliatePartner).filter(AffiliatePartner.id == attribution.partner_id).first()
    if not partner or partner.status not in {"active", "suspended", "pending_terms"}:
        audit(
            db,
            "commission.partner_inactive",
            partner_id=attribution.partner_id,
            subject_user_id=user.id,
            source_ref=invoice_id,
        )
        db.commit()
        return None
    terms = db.query(AffiliateProgramTerms).filter(AffiliateProgramTerms.id == attribution.terms_version_id).first()
    if not terms:
        logger.error("Affiliate attribution %s references missing terms", attribution.id)
        audit(
            db,
            "commission.terms_missing",
            partner_id=partner.id,
            subject_user_id=user.id,
            source_ref=invoice_id,
        )
        db.commit()
        return None

    source_key = f"invoice:{invoice_id}:accrual"
    existing = (
        db.query(AffiliateCommissionEntry)
        .filter(AffiliateCommissionEntry.source_key == source_key)
        .first()
    )
    if existing:
        return existing

    paid_at_value = _field(_field(invoice, "status_transitions"), "paid_at")
    paid_at = _stripe_datetime(paid_at_value or _field(invoice, "created"))
    if attribution.attributed_at and paid_at < attribution.attributed_at:
        audit(
            db,
            "commission.invoice_before_attribution",
            partner_id=partner.id,
            subject_user_id=user.id,
            source_ref=invoice_id,
            payload={"paid_at": paid_at.isoformat()},
        )
        db.commit()
        return None

    currency = str(_field(invoice, "currency") or "").lower()
    if currency != terms.currency.lower():
        partner.risk_status = "review"
        partner.hold_reason = "Invoice currency is not supported by the partner terms."
        audit(
            db,
            "commission.currency_held",
            partner_id=partner.id,
            subject_user_id=user.id,
            source_ref=invoice_id,
            payload={"invoice_currency": currency, "terms_currency": terms.currency},
        )
        db.commit()
        return None

    commissionable = invoice_commissionable_minor(invoice)
    rate_bps = (
        partner.custom_commission_rate_bps
        if partner.custom_commission_rate_bps is not None
        else terms.commission_rate_bps
    )
    amount = _round_rate(commissionable, rate_bps)
    if amount <= 0:
        audit(
            db,
            "commission.no_eligible_subscription_cash",
            partner_id=partner.id,
            subject_user_id=user.id,
            source_ref=invoice_id,
            payload={
                "amount_paid_minor": max(0, _int(_field(invoice, "amount_paid"))),
                "invoice_lines_complete": not bool(
                    _field(_field(invoice, "lines"), "has_more", False)
                ),
            },
        )
        db.commit()
        return None
    if attribution.first_paid_at is None:
        attribution.first_paid_at = paid_at
        duration = (
            partner.custom_commission_months
            if partner.custom_commission_months is not None
            else terms.commission_months
        )
        attribution.commission_ends_at = _add_months(paid_at, duration)
    if attribution.commission_ends_at and paid_at >= attribution.commission_ends_at:
        audit(
            db,
            "commission.outside_window",
            partner_id=partner.id,
            subject_user_id=user.id,
            source_ref=invoice_id,
        )
        db.commit()
        return None
    charge_id = _field(invoice, "charge")
    if charge_id is not None and not isinstance(charge_id, str):
        charge_id = _field(charge_id, "id")
    entry = AffiliateCommissionEntry(
        partner_id=partner.id,
        attribution_id=attribution.id,
        terms_version_id=terms.id,
        source_key=source_key,
        source_event_id=stripe_event_id,
        entry_type="accrual",
        stripe_invoice_id=invoice_id,
        stripe_charge_id=str(charge_id) if charge_id else None,
        amount_minor=amount,
        commissionable_minor=commissionable,
        rate_bps=rate_bps,
        currency=currency,
        available_at=_availability_date(paid_at, terms.hold_days),
        description="Commission on collected subscription revenue excluding tax",
    )
    db.add(entry)
    db.flush()
    db.add(
        AffiliateCommissionState(
            stripe_invoice_id=invoice_id,
            partner_id=partner.id,
            attribution_id=attribution.id,
            accrual_entry_id=entry.id,
            accrued_minor=amount,
            refund_target_minor=0,
            refund_target_commissionable_minor=0,
            dispute_active=False,
            projected_minor=amount,
        )
    )
    audit(
        db,
        "commission.accrued",
        partner_id=partner.id,
        subject_user_id=user.id,
        source_ref=invoice_id,
        payload={
            "entry_id": entry.id,
            "amount_minor": amount,
            "commissionable_minor": commissionable,
            "currency": currency,
        },
    )
    db.commit()
    db.refresh(entry)
    return entry


INVOICE_DECISION_EVENTS = {
    "commission.partner_inactive",
    "commission.terms_missing",
    "commission.currency_held",
    "commission.no_eligible_subscription_cash",
    "commission.invoice_before_attribution",
    "commission.outside_window",
}


def affiliate_invoice_decision_exists(db: Session, invoice_id: str) -> bool:
    """Whether a paid invoice was accrued or intentionally made ineligible."""
    if _invoice_accrual(db, invoice_id):
        return True
    return bool(
        db.query(AffiliateAuditEvent.id)
        .filter(
            AffiliateAuditEvent.source_ref == invoice_id,
            AffiliateAuditEvent.event_type.in_(INVOICE_DECISION_EVENTS),
        )
        .first()
    )


def user_has_active_affiliate_attribution(db: Session, user_id: int) -> bool:
    return bool(
        db.query(AffiliateAttribution.id)
        .filter(
            AffiliateAttribution.invitee_user_id == user_id,
            AffiliateAttribution.status == "active",
        )
        .first()
    )


def _invoice_accrual(db: Session, invoice_id: str) -> AffiliateCommissionEntry | None:
    return (
        db.query(AffiliateCommissionEntry)
        .filter(
            AffiliateCommissionEntry.stripe_invoice_id == invoice_id,
            AffiliateCommissionEntry.entry_type == "accrual",
        )
        .first()
    )


def _locked_commission_state(
    db: Session, accrual: AffiliateCommissionEntry
) -> AffiliateCommissionState:
    """Lock the invoice projection used to serialize competing money events.

    The fallback is defensive for records created during a rolling deployment.
    It derives a conservative projection from the append-only ledger and then
    persists it; new accruals always create the projection in the same commit.
    """
    state = (
        db.query(AffiliateCommissionState)
        .filter(AffiliateCommissionState.stripe_invoice_id == accrual.stripe_invoice_id)
        .with_for_update()
        .first()
    )
    if state:
        return state

    entries = (
        db.query(AffiliateCommissionEntry)
        .filter(AffiliateCommissionEntry.stripe_invoice_id == accrual.stripe_invoice_id)
        .all()
    )
    refund_target = max(
        0,
        -sum(
            entry.amount_minor
            for entry in entries
            if entry.entry_type == "refund_reversal"
        ),
    )
    refund_basis = max(
        0,
        -sum(
            entry.commissionable_minor
            for entry in entries
            if entry.entry_type == "refund_reversal"
        ),
    )
    dispute_net = sum(
        entry.amount_minor
        for entry in entries
        if entry.entry_type in {"dispute_reversal", "dispute_reinstatement"}
    )
    state = AffiliateCommissionState(
        stripe_invoice_id=accrual.stripe_invoice_id,
        partner_id=accrual.partner_id,
        attribution_id=accrual.attribution_id,
        accrual_entry_id=accrual.id,
        accrued_minor=accrual.amount_minor,
        refund_target_minor=min(accrual.amount_minor, refund_target),
        refund_target_commissionable_minor=min(
            accrual.commissionable_minor, refund_basis
        ),
        dispute_active=dispute_net < 0,
        projected_minor=max(0, sum(entry.amount_minor for entry in entries)),
    )
    db.add(state)
    db.flush()
    return state


def _desired_commission_minor(state: AffiliateCommissionState) -> int:
    effective_reversal = state.accrued_minor if state.dispute_active else state.refund_target_minor
    return max(0, state.accrued_minor - effective_reversal)


def record_refund(
    db: Session,
    *,
    invoice_id: str,
    charge_id: str | None,
    amount_refunded_minor: int,
    invoice_amount_paid_minor: int,
    stripe_event_id: str,
) -> AffiliateCommissionEntry | None:
    accrual = _invoice_accrual(db, invoice_id)
    if not accrual or invoice_amount_paid_minor <= 0:
        return None
    source_key = f"event:{stripe_event_id}:refund"
    existing = db.query(AffiliateCommissionEntry).filter(AffiliateCommissionEntry.source_key == source_key).first()
    if existing:
        return existing
    state = _locked_commission_state(db, accrual)
    refund_ratio = min(
        Decimal(1),
        Decimal(max(0, amount_refunded_minor)) / Decimal(invoice_amount_paid_minor),
    )
    target = int(
        (Decimal(accrual.amount_minor) * refund_ratio).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
    target_basis = int(
        (Decimal(accrual.commissionable_minor) * refund_ratio).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
    previous_target_basis = state.refund_target_commissionable_minor
    state.refund_target_minor = max(state.refund_target_minor, target)
    state.refund_target_commissionable_minor = max(
        state.refund_target_commissionable_minor, target_basis
    )
    desired = _desired_commission_minor(state)
    delta = state.projected_minor - desired
    state.projected_minor = desired
    if delta <= 0:
        audit(
            db,
            "commission.refund_observed",
            partner_id=accrual.partner_id,
            source_ref=source_key,
            payload={
                "amount_refunded_minor": amount_refunded_minor,
                "target_minor": state.refund_target_minor,
                "dispute_active": state.dispute_active,
            },
        )
        db.commit()
        return None
    entry = AffiliateCommissionEntry(
        partner_id=accrual.partner_id,
        attribution_id=accrual.attribution_id,
        terms_version_id=accrual.terms_version_id,
        source_key=source_key,
        source_event_id=stripe_event_id,
        entry_type="refund_reversal",
        stripe_invoice_id=invoice_id,
        stripe_charge_id=charge_id,
        amount_minor=-delta,
        commissionable_minor=-max(
            0,
            state.refund_target_commissionable_minor - previous_target_basis,
        ),
        rate_bps=accrual.rate_bps,
        currency=accrual.currency,
        available_at=utcnow(),
        description="Commission reversal for refunded subscription revenue",
    )
    db.add(entry)
    db.flush()
    audit(
        db,
        "commission.refund_reversed",
        partner_id=accrual.partner_id,
        source_ref=invoice_id,
        payload={"entry_id": entry.id, "amount_minor": -delta},
    )
    db.commit()
    db.refresh(entry)
    return entry


def record_dispute_opened(
    db: Session,
    *,
    invoice_id: str,
    charge_id: str,
    stripe_event_id: str,
) -> AffiliateCommissionEntry | None:
    accrual = _invoice_accrual(db, invoice_id)
    if not accrual:
        return None
    source_key = f"dispute:{charge_id}:opened"
    existing = db.query(AffiliateCommissionEntry).filter(AffiliateCommissionEntry.source_key == source_key).first()
    if existing:
        return existing
    state = _locked_commission_state(db, accrual)
    state.dispute_active = True
    desired = _desired_commission_minor(state)
    delta = state.projected_minor - desired
    state.projected_minor = desired
    if delta <= 0:
        audit(
            db,
            "commission.dispute_observed",
            partner_id=accrual.partner_id,
            source_ref=source_key,
            payload={"charge_id": charge_id},
        )
        db.commit()
        return None
    entry = AffiliateCommissionEntry(
        partner_id=accrual.partner_id,
        attribution_id=accrual.attribution_id,
        terms_version_id=accrual.terms_version_id,
        source_key=source_key,
        source_event_id=stripe_event_id,
        entry_type="dispute_reversal",
        stripe_invoice_id=invoice_id,
        stripe_charge_id=charge_id,
        amount_minor=-delta,
        commissionable_minor=-max(
            0,
            accrual.commissionable_minor
            - state.refund_target_commissionable_minor,
        ),
        rate_bps=accrual.rate_bps,
        currency=accrual.currency,
        available_at=utcnow(),
        description="Commission held because the customer disputed the charge",
    )
    db.add(entry)
    db.flush()
    audit(
        db,
        "commission.dispute_reversed",
        partner_id=accrual.partner_id,
        source_ref=invoice_id,
        payload={"entry_id": entry.id, "amount_minor": -delta},
    )
    db.commit()
    db.refresh(entry)
    return entry


def record_dispute_won(
    db: Session,
    *,
    invoice_id: str,
    charge_id: str,
    stripe_event_id: str,
) -> AffiliateCommissionEntry | None:
    accrual = _invoice_accrual(db, invoice_id)
    if not accrual:
        return None
    source_key = f"dispute:{charge_id}:won"
    existing = db.query(AffiliateCommissionEntry).filter(AffiliateCommissionEntry.source_key == source_key).first()
    if existing:
        return existing
    state = _locked_commission_state(db, accrual)
    if not state.dispute_active:
        return None
    state.dispute_active = False
    desired = _desired_commission_minor(state)
    delta = desired - state.projected_minor
    state.projected_minor = desired
    if delta <= 0:
        audit(
            db,
            "commission.dispute_won_observed",
            partner_id=accrual.partner_id,
            source_ref=source_key,
            payload={"charge_id": charge_id, "refund_target_minor": state.refund_target_minor},
        )
        db.commit()
        return None
    entry = AffiliateCommissionEntry(
        partner_id=accrual.partner_id,
        attribution_id=accrual.attribution_id,
        terms_version_id=accrual.terms_version_id,
        source_key=source_key,
        source_event_id=stripe_event_id,
        entry_type="dispute_reinstatement",
        stripe_invoice_id=invoice_id,
        stripe_charge_id=charge_id,
        amount_minor=delta,
        commissionable_minor=max(
            0,
            accrual.commissionable_minor
            - state.refund_target_commissionable_minor,
        ),
        rate_bps=accrual.rate_bps,
        currency=accrual.currency,
        available_at=utcnow(),
        description="Commission reinstated after a dispute was won",
    )
    db.add(entry)
    db.flush()
    audit(
        db,
        "commission.dispute_reinstated",
        partner_id=accrual.partner_id,
        source_ref=invoice_id,
        payload={"entry_id": entry.id, "amount_minor": entry.amount_minor},
    )
    db.commit()
    db.refresh(entry)
    return entry


def record_manual_adjustment(
    db: Session,
    *,
    partner: AffiliatePartner,
    attribution_id: int,
    amount_minor: int,
    reason: str,
    idempotency_key: str,
    admin: User,
) -> AffiliateCommissionEntry:
    """Append an auditable finance correction without editing earned history."""
    if amount_minor == 0 or abs(amount_minor) > 100_000_000:
        raise AffiliateProgramError(
            "invalid_amount", "Adjustment must be non-zero and within the operational limit."
        )
    explanation = (reason or "").strip()
    if len(explanation) < 20 or len(explanation) > 1000:
        raise AffiliateProgramError(
            "reason_required", "Record a 20 to 1,000 character adjustment reason."
        )
    key = (idempotency_key or "").strip()
    if not key or len(key) > 120:
        raise AffiliateProgramError("invalid_key", "A stable idempotency key is required.")
    source_key = f"manual:{partner.id}:{key}"
    existing = (
        db.query(AffiliateCommissionEntry)
        .filter(AffiliateCommissionEntry.source_key == source_key)
        .first()
    )
    if existing:
        return existing
    attribution = (
        db.query(AffiliateAttribution)
        .filter(
            AffiliateAttribution.id == attribution_id,
            AffiliateAttribution.partner_id == partner.id,
        )
        .first()
    )
    if not attribution:
        raise AffiliateProgramError("not_found", "Attribution does not belong to that partner.")
    terms = (
        db.query(AffiliateProgramTerms)
        .filter(AffiliateProgramTerms.id == attribution.terms_version_id)
        .first()
    )
    if not terms:
        raise AffiliateProgramError("terms_unavailable", "Attribution terms are unavailable.")
    entry = AffiliateCommissionEntry(
        partner_id=partner.id,
        attribution_id=attribution.id,
        terms_version_id=terms.id,
        source_key=source_key,
        entry_type="manual_adjustment",
        amount_minor=amount_minor,
        commissionable_minor=0,
        rate_bps=0,
        currency=terms.currency,
        available_at=utcnow(),
        description=explanation,
    )
    db.add(entry)
    db.flush()
    audit(
        db,
        "commission.manual_adjustment",
        actor_user_id=admin.id,
        partner_id=partner.id,
        source_ref=source_key,
        payload={
            "entry_id": entry.id,
            "attribution_id": attribution.id,
            "amount_minor": amount_minor,
        },
    )
    db.commit()
    db.refresh(entry)
    return entry


def _unassigned_entries_query(
    db: Session,
    *,
    partner_id: int,
    currency: str,
    through: datetime,
):
    return (
        db.query(AffiliateCommissionEntry)
        .outerjoin(
            AffiliatePayoutItem,
            AffiliatePayoutItem.commission_entry_id == AffiliateCommissionEntry.id,
        )
        .filter(
            AffiliateCommissionEntry.partner_id == partner_id,
            AffiliateCommissionEntry.currency == currency,
            AffiliateCommissionEntry.available_at <= through,
            AffiliatePayoutItem.id.is_(None),
        )
        .order_by(AffiliateCommissionEntry.created_at, AffiliateCommissionEntry.id)
    )


def payable_balance(db: Session, partner_id: int, currency: str = "usd") -> int:
    now = utcnow()
    entries = _unassigned_entries_query(
        db, partner_id=partner_id, currency=currency, through=now
    ).all()
    return sum(entry.amount_minor for entry in entries)


def _payout_country_is_supported(db: Session, partner: AffiliatePartner) -> bool:
    country = (
        db.query(AffiliateApplication.country_code)
        .filter(AffiliateApplication.id == partner.application_id)
        .scalar()
    )
    return bool(country and country in supported_countries())


def compliance_payload(profile: AffiliateComplianceProfile | None) -> dict:
    if profile is None:
        return {
            "tax_residency_country": None,
            "tax_form_type": None,
            "tax_verified_at": None,
            "sanctions_status": "pending",
            "sanctions_checked_at": None,
            "withholding_rate_bps": 0,
            "review_note": None,
            "ready": False,
        }
    return {
        "tax_residency_country": profile.tax_residency_country,
        "tax_form_type": profile.tax_form_type,
        "tax_verified_at": profile.tax_verified_at,
        "sanctions_status": profile.sanctions_status,
        "sanctions_checked_at": profile.sanctions_checked_at,
        "withholding_rate_bps": profile.withholding_rate_bps,
        "review_note": profile.review_note,
        "ready": bool(
            profile.tax_verified_at
            and profile.tax_form_reference_hash
            and profile.sanctions_status == "clear"
            and profile.sanctions_checked_at
        ),
    }


def update_partner_compliance(
    db: Session,
    *,
    partner: AffiliatePartner,
    admin: User,
    tax_residency_country: str,
    tax_form_type: str,
    tax_form_reference: str | None,
    tax_verified: bool,
    sanctions_status: str,
    withholding_rate_bps: int,
    review_note: str,
) -> AffiliateComplianceProfile:
    country = (tax_residency_country or "").strip().upper()
    if len(country) != 2 or not country.isalpha():
        raise AffiliateProgramError("invalid_compliance", "Tax residency must be a two-letter country code.")
    form_type = (tax_form_type or "").strip().lower()
    if form_type not in {"w9", "w8ben", "w8bene", "local_tax_residency", "other"}:
        raise AffiliateProgramError("invalid_compliance", "Tax form type is not recognized.")
    profile = (
        db.query(AffiliateComplianceProfile)
        .filter(AffiliateComplianceProfile.partner_id == partner.id)
        .with_for_update()
        .first()
    )
    reference = (tax_form_reference or "").strip()
    if tax_verified and len(reference) < 6 and not (profile and profile.tax_form_reference_hash):
        raise AffiliateProgramError("invalid_compliance", "Verified tax evidence requires a stable reference.")
    sanctions = (sanctions_status or "").strip().lower()
    if sanctions not in {"pending", "clear", "review", "blocked"}:
        raise AffiliateProgramError("invalid_compliance", "Sanctions status is invalid.")
    if not 0 <= withholding_rate_bps <= 10_000:
        raise AffiliateProgramError("invalid_compliance", "Withholding must be between 0% and 100%.")
    note = (review_note or "").strip()
    if len(note) < 20:
        raise AffiliateProgramError("notes_required", "Record at least 20 characters of compliance evidence.")
    if not profile:
        profile = AffiliateComplianceProfile(partner_id=partner.id)
        db.add(profile)
    now = utcnow()
    profile.tax_residency_country = country
    profile.tax_form_type = form_type
    profile.tax_form_reference_hash = (
        privacy_hash(reference)
        if tax_verified and reference
        else profile.tax_form_reference_hash
        if tax_verified
        else None
    )
    profile.tax_verified_at = now if tax_verified else None
    profile.tax_verified_by_user_id = admin.id if tax_verified else None
    profile.sanctions_status = sanctions
    profile.sanctions_checked_at = now if sanctions in {"clear", "review", "blocked"} else None
    profile.sanctions_checked_by_user_id = (
        admin.id if profile.sanctions_checked_at else None
    )
    profile.withholding_rate_bps = withholding_rate_bps
    profile.review_note = note[:4000]
    if sanctions == "blocked":
        partner.risk_status = "held"
        partner.hold_reason = "Compliance screening blocked affiliate payouts."
    elif sanctions == "review":
        partner.risk_status = "review"
        partner.hold_reason = "Compliance screening requires manual review."
    audit(
        db,
        "compliance.updated",
        actor_user_id=admin.id,
        partner_id=partner.id,
        source_ref=f"compliance:{partner.id}",
        payload={
            "country": country,
            "tax_form_type": form_type,
            "tax_verified": tax_verified,
            "sanctions_status": sanctions,
            "withholding_rate_bps": withholding_rate_bps,
        },
    )
    db.commit()
    db.refresh(profile)
    return profile


def payout_compliance_profile(
    db: Session,
    partner: AffiliatePartner,
) -> AffiliateComplianceProfile:
    profile = (
        db.query(AffiliateComplianceProfile)
        .filter(AffiliateComplianceProfile.partner_id == partner.id)
        .first()
    )
    if not profile or not compliance_payload(profile)["ready"]:
        raise AffiliateProgramError(
            "compliance_incomplete",
            "Tax verification and sanctions screening must be complete before payout.",
        )
    if profile.tax_residency_country not in supported_countries():
        raise AffiliateProgramError("unsupported_country", "This payout corridor is not enabled.")
    return profile


def create_payout_batch(
    db: Session,
    *,
    partner: AffiliatePartner,
    admin: User,
    through: datetime | None = None,
) -> AffiliatePayout:
    through = min(through or utcnow(), utcnow())
    partner = (
        db.query(AffiliatePartner)
        .filter(AffiliatePartner.id == partner.id)
        .with_for_update()
        .first()
    )
    if not partner or partner.status not in {"active", "closed"}:
        raise AffiliateProgramError("partner_inactive", "This partner is not eligible for payout.")
    if partner.risk_status != "clear":
        raise AffiliateProgramError("partner_held", "Resolve the partner risk hold before payout.")
    if not _payout_country_is_supported(db, partner):
        raise AffiliateProgramError(
            "unsupported_country", "This payout corridor is not enabled."
        )
    compliance = payout_compliance_profile(db, partner)
    terms = db.query(AffiliateProgramTerms).filter(AffiliateProgramTerms.id == partner.terms_version_id).first()
    if not terms:
        raise AffiliateProgramError("terms_unavailable", "Partner terms are unavailable.")
    entries = _unassigned_entries_query(
        db,
        partner_id=partner.id,
        currency=terms.currency,
        through=through,
    ).with_for_update(of=AffiliateCommissionEntry).all()
    gross_amount = sum(entry.amount_minor for entry in entries)
    if gross_amount < terms.payout_minimum_minor:
        raise AffiliateProgramError(
            "below_threshold",
            f"Payable balance is below the {terms.payout_minimum_minor}-minor-unit threshold.",
        )
    withholding = _round_rate(gross_amount, compliance.withholding_rate_bps)
    net_amount = gross_amount - withholding
    if net_amount <= 0:
        raise AffiliateProgramError(
            "compliance_incomplete",
            "Withholding leaves no transferable payout amount.",
        )
    payout = AffiliatePayout(
        partner_id=partner.id,
        currency=terms.currency,
        gross_amount_minor=gross_amount,
        withholding_rate_bps=compliance.withholding_rate_bps,
        withholding_minor=withholding,
        amount_minor=net_amount,
        threshold_minor=terms.payout_minimum_minor,
        status="draft",
        period_start=min((entry.created_at for entry in entries), default=None),
        period_end=through,
        created_by_user_id=admin.id,
    )
    db.add(payout)
    db.flush()
    for entry in entries:
        db.add(
            AffiliatePayoutItem(
                payout_id=payout.id,
                commission_entry_id=entry.id,
                amount_minor=entry.amount_minor,
            )
        )
    audit(
        db,
        "payout.drafted",
        actor_user_id=admin.id,
        partner_id=partner.id,
        source_ref=f"payout:{payout.id}",
        payload={
            "gross_amount_minor": gross_amount,
            "withholding_minor": withholding,
            "amount_minor": net_amount,
            "currency": terms.currency,
            "entries": len(entries),
        },
    )
    db.commit()
    db.refresh(payout)
    return payout


def approve_payout(db: Session, payout: AffiliatePayout, admin: User) -> AffiliatePayout:
    payout = (
        db.query(AffiliatePayout)
        .filter(AffiliatePayout.id == payout.id)
        .with_for_update(of=AffiliatePayout)
        .first()
    )
    if not payout:
        raise AffiliateProgramError("not_found", "Payout not found.")
    if payout.status != "draft":
        raise AffiliateProgramError("invalid_state", "Only draft payouts can be approved.")
    if payout.created_by_user_id == admin.id:
        raise AffiliateProgramError(
            "dual_control_required", "A different administrator must approve this payout."
        )
    partner = (
        db.query(AffiliatePartner)
        .filter(AffiliatePartner.id == payout.partner_id)
        .with_for_update(of=AffiliatePartner)
        .first()
    )
    if not partner or partner.status not in {"active", "closed"} or partner.risk_status != "clear":
        raise AffiliateProgramError("partner_held", "Partner is not eligible for payout.")
    if not _payout_country_is_supported(db, partner):
        raise AffiliateProgramError(
            "unsupported_country", "This payout corridor is not enabled."
        )
    compliance = payout_compliance_profile(db, partner)

    # Refunds can land after finance drafts a batch. Pull any newly available
    # negative entries into the draft before approval; otherwise the transfer
    # can pay a balance the append-only ledger has already reversed.
    late_negatives = (
        _unassigned_entries_query(
            db,
            partner_id=payout.partner_id,
            currency=payout.currency,
            through=utcnow(),
        )
        .filter(AffiliateCommissionEntry.amount_minor < 0)
        .with_for_update(of=AffiliateCommissionEntry)
        .all()
    )
    if late_negatives:
        adjusted_gross = payout.gross_amount_minor + sum(
            entry.amount_minor for entry in late_negatives
        )
        if adjusted_gross < payout.threshold_minor:
            db.query(AffiliatePayoutItem).filter(
                AffiliatePayoutItem.payout_id == payout.id
            ).delete(synchronize_session=False)
            payout.status = "canceled"
            payout.failure_reason = "Balance fell below the payout threshold before approval."
            audit(
                db,
                "payout.canceled_balance_changed",
                actor_user_id=admin.id,
                partner_id=payout.partner_id,
                source_ref=f"payout:{payout.id}",
                payload={"adjusted_gross_amount_minor": adjusted_gross},
            )
            db.commit()
            raise AffiliateProgramError(
                "balance_changed",
                "The payable balance changed after drafting; this batch was canceled.",
            )
        for entry in late_negatives:
            db.add(
                AffiliatePayoutItem(
                    payout_id=payout.id,
                    commission_entry_id=entry.id,
                    amount_minor=entry.amount_minor,
                )
            )
        payout.gross_amount_minor = adjusted_gross
        payout.withholding_rate_bps = compliance.withholding_rate_bps
        payout.withholding_minor = _round_rate(
            adjusted_gross,
            compliance.withholding_rate_bps,
        )
        payout.amount_minor = adjusted_gross - payout.withholding_minor
        payout.period_end = utcnow()
    elif payout.withholding_rate_bps != compliance.withholding_rate_bps:
        payout.withholding_rate_bps = compliance.withholding_rate_bps
        payout.withholding_minor = _round_rate(
            payout.gross_amount_minor,
            compliance.withholding_rate_bps,
        )
        payout.amount_minor = payout.gross_amount_minor - payout.withholding_minor
    if payout.amount_minor <= 0:
        raise AffiliateProgramError("compliance_incomplete", "Withholding leaves no transferable amount.")
    payout.status = "approved"
    payout.approved_by_user_id = admin.id
    payout.approved_at = utcnow()
    audit(
        db,
        "payout.approved",
        actor_user_id=admin.id,
        partner_id=payout.partner_id,
        source_ref=f"payout:{payout.id}",
    )
    db.commit()
    db.refresh(payout)
    return payout


def execute_payout(db: Session, payout: AffiliatePayout, admin: User) -> AffiliatePayout:
    if not payouts_runtime_enabled():
        raise AffiliateProgramError(
            "payouts_disabled", "Set AFFILIATE_PAYOUTS_ENABLED only after finance launch approval."
        )
    payout = (
        db.query(AffiliatePayout)
        .filter(AffiliatePayout.id == payout.id)
        .with_for_update()
        .first()
    )
    if not payout:
        raise AffiliateProgramError("not_found", "Payout not found.")
    if payout.status == "paid":
        return payout
    if payout.status not in {"approved", "failed"}:
        raise AffiliateProgramError(
            "invalid_state", "Payout must be approved before execution."
        )
    partner = (
        db.query(AffiliatePartner)
        .filter(AffiliatePartner.id == payout.partner_id)
        .with_for_update()
        .first()
    )
    if not partner or partner.status not in {"active", "closed"} or partner.risk_status != "clear":
        raise AffiliateProgramError("partner_held", "Partner is not eligible for payout.")
    if not _payout_country_is_supported(db, partner):
        raise AffiliateProgramError(
            "unsupported_country", "This payout corridor is not enabled."
        )
    compliance = payout_compliance_profile(db, partner)
    if compliance.withholding_rate_bps != payout.withholding_rate_bps:
        raise AffiliateProgramError(
            "compliance_changed",
            "Compliance withholding changed after approval; cancel and redraft the payout.",
        )
    terms = (
        db.query(AffiliateProgramTerms)
        .filter(AffiliateProgramTerms.id == partner.terms_version_id)
        .first()
    )
    if not legal_launch_ready() or not launch_approval_state(db, terms)["ready"]:
        raise AffiliateProgramError(
            "launch_approvals_missing",
            "Launch approvals are no longer valid for this partner's terms.",
        )
    item_total = (
        db.query(func.coalesce(func.sum(AffiliatePayoutItem.amount_minor), 0))
        .filter(AffiliatePayoutItem.payout_id == payout.id)
        .scalar()
        or 0
    )
    if item_total != payout.gross_amount_minor:
        db.query(AffiliatePayoutItem).filter(
            AffiliatePayoutItem.payout_id == payout.id
        ).delete(synchronize_session=False)
        payout.status = "canceled"
        payout.failure_reason = "Payout item total did not match the approved batch amount."
        audit(
            db,
            "payout.canceled_reconciliation_mismatch",
            actor_user_id=admin.id,
            partner_id=partner.id,
            source_ref=f"payout:{payout.id}",
            payload={
                "item_total_minor": item_total,
                "batch_gross_amount_minor": payout.gross_amount_minor,
            },
        )
        db.commit()
        raise AffiliateProgramError(
            "balance_changed", "The approved payout no longer reconciles to its ledger items."
        )
    late_negative = None
    if payout.status == "approved":
        late_negative = (
            _unassigned_entries_query(
                db,
                partner_id=payout.partner_id,
                currency=payout.currency,
                through=utcnow(),
            )
            .filter(AffiliateCommissionEntry.amount_minor < 0)
            .with_for_update(of=AffiliateCommissionEntry)
            .first()
        )
    if late_negative:
        db.query(AffiliatePayoutItem).filter(
            AffiliatePayoutItem.payout_id == payout.id
        ).delete(synchronize_session=False)
        payout.status = "canceled"
        payout.failure_reason = "Balance changed after approval; a new payout review is required."
        audit(
            db,
            "payout.canceled_balance_changed",
            actor_user_id=admin.id,
            partner_id=partner.id,
            source_ref=f"payout:{payout.id}",
        )
        db.commit()
        raise AffiliateProgramError(
            "balance_changed",
            "The payable balance changed after approval; this batch was canceled.",
        )
    # Once a Stripe call has been attempted, a timeout may mean either failure
    # or success. A retry must preserve the exact approved amount and reuse the
    # idempotency key; newly arrived negatives carry forward. Canceling or
    # changing a failed batch here could strand a successful Stripe transfer
    # and make its positive entries eligible for payment a second time.
    if not partner.stripe_connect_account_id:
        raise AffiliateProgramError("connect_incomplete", "Partner payout verification is incomplete.")
    if not os.getenv("STRIPE_SECRET_KEY"):
        raise AffiliateProgramError("stripe_unavailable", "Stripe is not configured.")

    import stripe

    stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
    try:
        connect_account = stripe.Account.retrieve(partner.stripe_connect_account_id)
    except Exception:
        logger.exception("Affiliate Connect preflight failed payout=%s", payout.id)
        raise AffiliateProgramError(
            "stripe_unavailable", "Stripe payout verification could not be refreshed."
        ) from None
    connect_ready = bool(_field(connect_account, "payouts_enabled"))
    if partner.payouts_enabled != connect_ready:
        partner.payouts_enabled = connect_ready
    if not connect_ready:
        audit(
            db,
            "payout.connect_not_ready",
            actor_user_id=admin.id,
            partner_id=partner.id,
            source_ref=f"payout:{payout.id}",
        )
        db.commit()
        raise AffiliateProgramError(
            "connect_incomplete", "Partner payout verification is incomplete."
        )
    payout.status = "processing"
    payout.processed_at = utcnow()
    # Keep the row lock and transaction open through the transfer call. If the
    # process dies after Stripe succeeds, PostgreSQL rolls this status back to
    # approved and the next attempt safely reuses the payout idempotency key.
    db.flush()
    try:
        transfer = stripe.Transfer.create(
            amount=payout.amount_minor,
            currency=payout.currency,
            destination=partner.stripe_connect_account_id,
            transfer_group=f"affiliate_payout_{payout.id}",
            metadata={"affiliate_payout_id": str(payout.id), "partner_id": str(partner.id)},
            description=f"Editube affiliate payout #{payout.id}",
            idempotency_key=f"affiliate-payout-{payout.id}",
        )
    except Exception as exc:
        logger.exception("Affiliate payout transfer failed payout=%s", payout.id)
        payout.status = "failed"
        payout.failure_reason = str(exc)[:1000]
        audit(
            db,
            "payout.failed",
            actor_user_id=admin.id,
            partner_id=partner.id,
            source_ref=f"payout:{payout.id}",
            payload={"error_type": type(exc).__name__},
        )
        db.commit()
        raise AffiliateProgramError("transfer_failed", "Stripe could not create the transfer.") from None

    payout.stripe_transfer_id = str(_field(transfer, "id"))
    payout.status = "paid"
    payout.paid_at = utcnow()
    payout.failure_reason = None
    audit(
        db,
        "payout.paid",
        actor_user_id=admin.id,
        partner_id=partner.id,
        source_ref=f"payout:{payout.id}",
        payload={"stripe_transfer_id": payout.stripe_transfer_id, "amount_minor": payout.amount_minor},
    )
    db.commit()
    db.refresh(payout)
    return payout


def partner_dashboard(db: Session, partner: AffiliatePartner) -> dict:
    now = utcnow()
    terms = db.query(AffiliateProgramTerms).filter(AffiliateProgramTerms.id == partner.terms_version_id).first()
    clicks = db.query(func.count(AffiliateClick.id)).filter(AffiliateClick.partner_id == partner.id).scalar() or 0
    referrals = db.query(func.count(AffiliateAttribution.id)).filter(AffiliateAttribution.partner_id == partner.id).scalar() or 0
    customers = (
        db.query(func.count(AffiliateAttribution.id))
        .filter(
            AffiliateAttribution.partner_id == partner.id,
            AffiliateAttribution.first_paid_at.isnot(None),
        )
        .scalar()
        or 0
    )
    entries = (
        db.query(AffiliateCommissionEntry)
        .filter(AffiliateCommissionEntry.partner_id == partner.id)
        .order_by(AffiliateCommissionEntry.created_at.desc(), AffiliateCommissionEntry.id.desc())
        .limit(100)
        .all()
    )
    accrued = (
        db.query(func.coalesce(func.sum(AffiliateCommissionEntry.amount_minor), 0))
        .filter(AffiliateCommissionEntry.partner_id == partner.id)
        .scalar()
        or 0
    )
    pending = (
        db.query(func.coalesce(func.sum(AffiliateCommissionEntry.amount_minor), 0))
        .filter(
            AffiliateCommissionEntry.partner_id == partner.id,
            AffiliateCommissionEntry.available_at > now,
        )
        .scalar()
        or 0
    )
    reversed_total = (
        db.query(func.coalesce(func.sum(AffiliateCommissionEntry.amount_minor), 0))
        .filter(
            AffiliateCommissionEntry.partner_id == partner.id,
            AffiliateCommissionEntry.amount_minor < 0,
        )
        .scalar()
        or 0
    )
    totals = {
        "accrued_minor": accrued,
        "pending_minor": pending,
        "payable_minor": payable_balance(db, partner.id, terms.currency if terms else "usd"),
        "paid_minor": (
            db.query(func.coalesce(func.sum(AffiliatePayout.amount_minor), 0))
            .filter(AffiliatePayout.partner_id == partner.id, AffiliatePayout.status == "paid")
            .scalar()
            or 0
        ),
        "reversed_minor": -reversed_total,
    }
    payouts = (
        db.query(AffiliatePayout)
        .filter(AffiliatePayout.partner_id == partner.id)
        .order_by(AffiliatePayout.created_at.desc(), AffiliatePayout.id.desc())
        .limit(50)
        .all()
    )
    campaign_click_rows = (
        db.query(AffiliateClick.campaign, func.count(AffiliateClick.id))
        .filter(AffiliateClick.partner_id == partner.id)
        .group_by(AffiliateClick.campaign)
        .order_by(func.count(AffiliateClick.id).desc())
        .limit(50)
        .all()
    )
    campaign_referrals = dict(
        db.query(AffiliateClick.campaign, func.count(AffiliateAttribution.id))
        .join(AffiliateAttribution, AffiliateAttribution.click_id == AffiliateClick.id)
        .filter(AffiliateClick.partner_id == partner.id)
        .group_by(AffiliateClick.campaign)
        .all()
    )
    campaign_customers = dict(
        db.query(AffiliateClick.campaign, func.count(AffiliateAttribution.id))
        .join(AffiliateAttribution, AffiliateAttribution.click_id == AffiliateClick.id)
        .filter(
            AffiliateClick.partner_id == partner.id,
            AffiliateAttribution.first_paid_at.isnot(None),
        )
        .group_by(AffiliateClick.campaign)
        .all()
    )
    campaign_commission = dict(
        db.query(
            AffiliateClick.campaign,
            func.coalesce(func.sum(AffiliateCommissionEntry.amount_minor), 0),
        )
        .join(AffiliateAttribution, AffiliateAttribution.click_id == AffiliateClick.id)
        .join(
            AffiliateCommissionEntry,
            AffiliateCommissionEntry.attribution_id == AffiliateAttribution.id,
        )
        .filter(AffiliateClick.partner_id == partner.id)
        .group_by(AffiliateClick.campaign)
        .all()
    )
    click_counts = dict(campaign_click_rows)
    managed_campaigns = list_partner_campaigns(db, partner)
    campaigns = [
        {
            **campaign_payload(item, partner.code),
            "campaign": item.slug,
            "clicks": int(click_counts.get(item.slug, 0)),
            "referrals": int(campaign_referrals.get(item.slug, 0)),
            "customers": int(campaign_customers.get(item.slug, 0)),
            "commission_minor": int(campaign_commission.get(item.slug, 0)),
        }
        for item in managed_campaigns
    ]
    if None in click_counts:
        campaigns.append(
            {
                "id": None,
                "slug": None,
                "name": "Direct link",
                "destination_path": "/signup",
                "status": "active",
                "link": build_partner_link(partner.code),
                "created_at": None,
                "updated_at": None,
                "campaign": None,
                "clicks": int(click_counts.get(None, 0)),
                "referrals": int(campaign_referrals.get(None, 0)),
                "customers": int(campaign_customers.get(None, 0)),
                "commission_minor": int(campaign_commission.get(None, 0)),
            }
        )
    return {
        "partner": partner,
        "terms": terms,
        "link": build_partner_link(partner.code),
        "metrics": {
            "clicks": clicks,
            "referrals": referrals,
            "customers": customers,
            "conversion_rate": round(customers / clicks, 4) if clicks else 0,
            **totals,
        },
        "entries": entries,
        "payouts": payouts,
        "campaigns": campaigns,
    }
