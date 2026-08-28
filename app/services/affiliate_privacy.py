"""Data-minimization operations for affiliate attribution evidence."""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.db.models import (
    AffiliateAttribution,
    AffiliateAuditEvent,
    AffiliateClick,
    AffiliateTermsAcceptance,
    Referral,
)
from app.services.affiliate_program import utcnow


def _positive_days(name: str, default: int, *, minimum: int) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default)) or str(default)))
    except ValueError:
        return default


def _scrub_click(click: AffiliateClick, now: datetime) -> None:
    # The row and timestamp remain as financial attribution evidence. Browser,
    # campaign, landing, and token-level correlators do not.
    click.token = f"retired_{click.id}_{secrets.token_urlsafe(16)}"
    click.campaign = None
    click.landing_path = None
    click.referrer_host = None
    click.ip_hash = None
    click.user_agent_hash = None
    click.risk_flags = None
    click.privacy_scrubbed_at = now


def apply_affiliate_privacy_retention(
    db: Session,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Scrub expired click details and aged acceptance network hashes."""
    now = now or utcnow()
    click_days = _positive_days(
        "AFFILIATE_CLICK_DETAIL_RETENTION_DAYS",
        90,
        minimum=60,
    )
    acceptance_days = _positive_days(
        "AFFILIATE_ACCEPTANCE_HASH_RETENTION_DAYS",
        90,
        minimum=30,
    )
    referral_email_days = _positive_days(
        "REFERRAL_INVITE_EMAIL_RETENTION_DAYS",
        30,
        minimum=1,
    )
    clicks = (
        db.query(AffiliateClick)
        .filter(
            AffiliateClick.privacy_scrubbed_at.is_(None),
            AffiliateClick.occurred_at < now - timedelta(days=click_days),
            AffiliateClick.expires_at <= now,
        )
        .limit(5_000)
        .all()
    )
    for click in clicks:
        _scrub_click(click, now)

    acceptances = (
        db.query(AffiliateTermsAcceptance)
        .filter(
            AffiliateTermsAcceptance.accepted_at
            < now - timedelta(days=acceptance_days),
            (
                AffiliateTermsAcceptance.ip_hash.isnot(None)
                | AffiliateTermsAcceptance.user_agent_hash.isnot(None)
            ),
        )
        .limit(5_000)
        .all()
    )
    for acceptance in acceptances:
        acceptance.ip_hash = None
        acceptance.user_agent_hash = None

    referral_invites = (
        db.query(Referral)
        .filter(
            Referral.invitee_user_id.is_(None),
            Referral.invitee_email.isnot(None),
            Referral.invite_expires_at.isnot(None),
            Referral.invite_expires_at
            <= now - timedelta(days=referral_email_days),
        )
        .limit(5_000)
        .all()
    )
    for referral in referral_invites:
        referral.invitee_email = None
        if referral.status == "invited":
            referral.status = "expired"

    if clicks or acceptances or referral_invites:
        db.add(
            AffiliateAuditEvent(
                event_type="privacy.retention_sweep",
                payload={
                    "clicks_scrubbed": len(clicks),
                    "acceptances_scrubbed": len(acceptances),
                    "referral_invite_emails_scrubbed": len(referral_invites),
                },
            )
        )
        db.commit()
    return {
        "clicks_scrubbed": len(clicks),
        "acceptances_scrubbed": len(acceptances),
        "referral_invite_emails_scrubbed": len(referral_invites),
    }


def scrub_affiliate_data_for_deleted_user(
    db: Session,
    *,
    user_id: int,
    now: datetime | None = None,
) -> dict[str, int]:
    """Immediately remove correlators tied to a deleted customer's account."""
    now = now or utcnow()
    clicks = (
        db.query(AffiliateClick)
        .join(AffiliateAttribution, AffiliateAttribution.click_id == AffiliateClick.id)
        .filter(
            AffiliateAttribution.invitee_user_id == user_id,
            AffiliateClick.privacy_scrubbed_at.is_(None),
        )
        .all()
    )
    for click in clicks:
        _scrub_click(click, now)
    acceptances = (
        db.query(AffiliateTermsAcceptance)
        .filter(AffiliateTermsAcceptance.accepted_by_user_id == user_id)
        .all()
    )
    for acceptance in acceptances:
        acceptance.ip_hash = None
        acceptance.user_agent_hash = None
    referrals = (
        db.query(Referral)
        .filter(
            Referral.invitee_user_id == user_id,
            Referral.invitee_email.isnot(None),
        )
        .all()
    )
    for referral in referrals:
        referral.invitee_email = None
    return {
        "clicks_scrubbed": len(clicks),
        "acceptances_scrubbed": len(acceptances),
        "referral_invite_emails_scrubbed": len(referrals),
    }
