"""Affiliate ledger invariants, Stripe verification, and monitor delivery."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from urllib.request import Request, urlopen

from sqlalchemy.orm import Session

from app.db.models import (
    AccountCreditLedger,
    AffiliateAuditEvent,
    AffiliateAttribution,
    AffiliateClick,
    AffiliateComplianceProfile,
    AffiliateCommissionEntry,
    AffiliateCommissionState,
    AffiliatePartner,
    AffiliatePayout,
    AffiliatePayoutItem,
    AffiliateProgramTerms,
    AffiliateTermsAcceptance,
    Referral,
)
from app.services.affiliate_program import (
    affiliate_invoice_decision_exists,
    invoice_commissionable_minor,
    compliance_payload,
    launch_approval_state,
    utcnow,
)
from app.services.affiliate_stripe import (
    invoice_with_complete_lines,
    stripe_field,
    user_for_invoice,
)

logger = logging.getLogger(__name__)
_health_cache_lock = threading.Lock()
_health_cache: dict[int, tuple[float, dict]] = {}


def _issue(
    severity: str,
    code: str,
    message: str,
    *,
    entity_type: str | None = None,
    entity_id: int | str | None = None,
    expected=None,
    actual=None,
) -> dict:
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "expected": expected,
        "actual": actual,
    }


def _finish(issues: list[dict], *, checked: dict[str, int]) -> dict:
    counts = Counter(issue["severity"] for issue in issues)
    status = "critical" if counts["critical"] else "degraded" if counts["warning"] else "ok"
    return {
        "status": status,
        "generated_at": utcnow().isoformat() + "Z",
        "checked": checked,
        "issue_counts": {
            "critical": counts["critical"],
            "warning": counts["warning"],
            "info": counts["info"],
        },
        "issues": issues,
    }


def database_reconciliation_report(db: Session) -> dict:
    """Verify invariants that must hold before any payout is trusted."""
    issues: list[dict] = []

    terms_rows = db.query(AffiliateProgramTerms).all()
    for terms in terms_rows:
        actual_checksum = hashlib.sha256((terms.legal_text or "").encode("utf-8")).hexdigest()
        if actual_checksum != terms.legal_copy_checksum:
            issues.append(
                _issue(
                    "critical",
                    "terms_checksum_mismatch",
                    "Stored affiliate terms no longer match their immutable checksum.",
                    entity_type="terms",
                    entity_id=terms.id,
                    expected=terms.legal_copy_checksum,
                    actual=actual_checksum,
                )
            )
        if terms.status == "active":
            approvals = launch_approval_state(db, terms)
            if not approvals["ready"]:
                issues.append(
                    _issue(
                        "critical",
                        "active_terms_missing_launch_approvals",
                        "Active affiliate terms no longer have every required approval.",
                        entity_type="terms",
                        entity_id=terms.id,
                        expected="legal, finance, product, engineering with separation of duties",
                        actual={
                            "missing_roles": approvals["missing_roles"],
                            "separation_of_duties": approvals["separation_of_duties"],
                        },
                    )
                )

    states = db.query(AffiliateCommissionState).all()
    for state in states:
        entries = (
            db.query(AffiliateCommissionEntry)
            .filter(AffiliateCommissionEntry.stripe_invoice_id == state.stripe_invoice_id)
            .all()
        )
        ledger_total = sum(entry.amount_minor for entry in entries)
        if ledger_total != state.projected_minor:
            issues.append(
                _issue(
                    "critical",
                    "invoice_projection_mismatch",
                    "Invoice ledger entries do not reconcile to the commission projection.",
                    entity_type="commission_state",
                    entity_id=state.id,
                    expected=state.projected_minor,
                    actual=ledger_total,
                )
            )
        accrual = next((entry for entry in entries if entry.id == state.accrual_entry_id), None)
        if not accrual or accrual.amount_minor != state.accrued_minor:
            issues.append(
                _issue(
                    "critical",
                    "accrual_state_mismatch",
                    "The immutable accrual does not match its projection seed.",
                    entity_type="commission_state",
                    entity_id=state.id,
                    expected=state.accrued_minor,
                    actual=accrual.amount_minor if accrual else None,
                )
            )

    rewarded_referrals = (
        db.query(Referral)
        .filter(Referral.reward_source_invoice_id.isnot(None))
        .all()
    )
    for referral in rewarded_referrals:
        credit_entries = (
            db.query(AccountCreditLedger)
            .filter(
                AccountCreditLedger.user_id == referral.referrer_user_id,
                AccountCreditLedger.source_ref.like(f"referral:{referral.id}%"),
            )
            .all()
        )
        actual_credits = sum(entry.delta for entry in credit_entries)
        expected_credits = (
            referral.reward_credits
            if referral.status == "rewarded"
            and not referral.reward_dispute_active
            and referral.reward_reversed_at is None
            else 0
        )
        if actual_credits != expected_credits:
            issues.append(
                _issue(
                    "critical",
                    "referral_reward_ledger_mismatch",
                    "Referral reward state does not reconcile to the account credit ledger.",
                    entity_type="referral",
                    entity_id=referral.id,
                    expected=expected_credits,
                    actual=actual_credits,
                )
            )
    orphan_accruals = (
        db.query(AffiliateCommissionEntry)
        .outerjoin(
            AffiliateCommissionState,
            AffiliateCommissionState.accrual_entry_id == AffiliateCommissionEntry.id,
        )
        .filter(
            AffiliateCommissionEntry.entry_type == "accrual",
            AffiliateCommissionState.id.is_(None),
        )
        .all()
    )
    for entry in orphan_accruals:
        issues.append(
            _issue(
                "critical",
                "accrual_projection_missing",
                "An invoice accrual has no refund/dispute projection.",
                entity_type="commission_entry",
                entity_id=entry.id,
            )
        )

    payouts = db.query(AffiliatePayout).all()
    for payout in payouts:
        items = (
            db.query(AffiliatePayoutItem, AffiliateCommissionEntry)
            .join(
                AffiliateCommissionEntry,
                AffiliateCommissionEntry.id == AffiliatePayoutItem.commission_entry_id,
            )
            .filter(AffiliatePayoutItem.payout_id == payout.id)
            .all()
        )
        item_total = sum(item.amount_minor for item, _entry in items)
        if payout.status != "canceled" and item_total != payout.gross_amount_minor:
            issues.append(
                _issue(
                    "critical",
                    "payout_item_total_mismatch",
                    "Payout gross amount does not equal its immutable item total.",
                    entity_type="payout",
                    entity_id=payout.id,
                    expected=payout.gross_amount_minor,
                    actual=item_total,
                )
            )
        expected_withholding = int(
            (
                Decimal(payout.gross_amount_minor)
                * Decimal(payout.withholding_rate_bps)
                / Decimal(10_000)
            ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        if (
            payout.status != "canceled"
            and (
                payout.withholding_minor != expected_withholding
                or payout.amount_minor
                != payout.gross_amount_minor - payout.withholding_minor
            )
        ):
            issues.append(
                _issue(
                    "critical",
                    "payout_withholding_mismatch",
                    "Payout gross, withholding, and net amounts do not reconcile.",
                    entity_type="payout",
                    entity_id=payout.id,
                    expected={
                        "withholding_minor": expected_withholding,
                        "amount_minor": payout.gross_amount_minor - expected_withholding,
                    },
                    actual={
                        "withholding_minor": payout.withholding_minor,
                        "amount_minor": payout.amount_minor,
                    },
                )
            )
        for item, entry in items:
            if entry.partner_id != payout.partner_id or entry.currency != payout.currency:
                issues.append(
                    _issue(
                        "critical",
                        "payout_item_scope_mismatch",
                        "A payout item belongs to another partner or currency.",
                        entity_type="payout_item",
                        entity_id=item.id,
                    )
                )
        if payout.status == "paid" and not payout.stripe_transfer_id:
            issues.append(
                _issue(
                    "critical",
                    "paid_payout_missing_transfer",
                    "A paid payout has no Stripe transfer reference.",
                    entity_type="payout",
                    entity_id=payout.id,
                )
            )
        if payout.status == "processing" and payout.processed_at and payout.processed_at < utcnow() - timedelta(minutes=15):
            issues.append(
                _issue(
                    "warning",
                    "payout_processing_stale",
                    "A payout has remained processing for more than 15 minutes.",
                    entity_type="payout",
                    entity_id=payout.id,
                )
            )
        if payout.status == "failed":
            issues.append(
                _issue(
                    "warning",
                    "payout_failed",
                    "A payout batch failed and requires finance review or an idempotent retry.",
                    entity_type="payout",
                    entity_id=payout.id,
                )
            )
        if payout.status == "canceled" and payout.created_at >= utcnow() - timedelta(days=7):
            issues.append(
                _issue(
                    "warning",
                    "payout_recently_canceled",
                    "A recently canceled payout batch requires finance review.",
                    entity_type="payout",
                    entity_id=payout.id,
                )
            )

    partners = db.query(AffiliatePartner).all()
    recent_click_count = 0
    for partner in partners:
        if partner.status == "active":
            accepted = (
                db.query(AffiliateTermsAcceptance.id)
                .filter(
                    AffiliateTermsAcceptance.partner_id == partner.id,
                    AffiliateTermsAcceptance.terms_version_id == partner.terms_version_id,
                )
                .first()
            )
            if not accepted:
                issues.append(
                    _issue(
                        "critical",
                        "active_partner_missing_acceptance",
                        "An active partner has not accepted the assigned terms version.",
                        entity_type="partner",
                        entity_id=partner.id,
                    )
                )
        if partner.payouts_enabled and not partner.stripe_connect_account_id:
            issues.append(
                _issue(
                    "critical",
                    "payout_enabled_without_connect",
                    "Partner payout readiness is true without a Connect account.",
                    entity_type="partner",
                    entity_id=partner.id,
                )
            )
        compliance = (
            db.query(AffiliateComplianceProfile)
            .filter(AffiliateComplianceProfile.partner_id == partner.id)
            .first()
        )
        if partner.payouts_enabled and not compliance_payload(compliance)["ready"]:
            issues.append(
                _issue(
                    "critical",
                    "payout_enabled_without_compliance",
                    "Partner payout readiness is true without tax and sanctions clearance.",
                    entity_type="partner",
                    entity_id=partner.id,
                )
            )
        unreviewed_risky_attributions = (
            db.query(AffiliateAttribution.id)
            .filter(
                AffiliateAttribution.partner_id == partner.id,
                AffiliateAttribution.status == "active",
                AffiliateAttribution.risk_flags.isnot(None),
            )
            .count()
        )
        if unreviewed_risky_attributions and partner.risk_status == "clear":
            issues.append(
                _issue(
                    "critical",
                    "risky_attribution_partner_clear",
                    "A clear partner has active attributions carrying controlled-identity risk signals.",
                    entity_type="partner",
                    entity_id=partner.id,
                    actual=unreviewed_risky_attributions,
                )
            )
        if partner.risk_status != "clear" and len((partner.hold_reason or "").strip()) < 20:
            issues.append(
                _issue(
                    "warning",
                    "risk_reason_missing",
                    "A partner review or hold has no useful investigation reason.",
                    entity_type="partner",
                    entity_id=partner.id,
                )
            )
        recent_clicks = (
            db.query(AffiliateClick)
            .filter(
                AffiliateClick.partner_id == partner.id,
                AffiliateClick.occurred_at >= utcnow() - timedelta(hours=24),
            )
            .all()
        )
        recent_click_count += len(recent_clicks)
        flagged_clicks = sum(bool(click.risk_flags) for click in recent_clicks)
        recent_attributions = (
            db.query(AffiliateAttribution.id)
            .filter(
                AffiliateAttribution.partner_id == partner.id,
                AffiliateAttribution.attributed_at >= utcnow() - timedelta(hours=24),
            )
            .count()
        )
        if len(recent_clicks) >= 100 and flagged_clicks * 10 >= len(recent_clicks):
            issues.append(
                _issue(
                    "warning",
                    "abnormal_click_velocity",
                    "At least ten percent of this partner's recent clicks carry a velocity risk flag.",
                    entity_type="partner",
                    entity_id=partner.id,
                    expected="<10% flagged",
                    actual={"clicks": len(recent_clicks), "flagged": flagged_clicks},
                )
            )
        if len(recent_clicks) >= 500 and recent_attributions == 0:
            issues.append(
                _issue(
                    "warning",
                    "abnormal_zero_conversion",
                    "A high-volume partner has no recent account attributions.",
                    entity_type="partner",
                    entity_id=partner.id,
                    actual={"clicks": len(recent_clicks), "attributions": 0},
                )
            )

    since = utcnow() - timedelta(hours=24)
    no_cash_count = (
        db.query(AffiliateAuditEvent)
        .filter(
            AffiliateAuditEvent.event_type == "commission.no_eligible_subscription_cash",
            AffiliateAuditEvent.created_at >= since,
        )
        .count()
    )
    if no_cash_count >= 5:
        issues.append(
            _issue(
                "warning",
                "repeated_non_commissionable_invoices",
                "Five or more attributed invoices produced no eligible subscription cash in 24 hours.",
                expected="<5",
                actual=no_cash_count,
            )
        )

    return _finish(
        issues,
        checked={
            "terms": len(terms_rows),
            "commission_states": len(states),
            "rewarded_referrals": len(rewarded_referrals),
            "payouts": len(payouts),
            "partners": len(partners),
            "recent_clicks": recent_click_count,
        },
    )


def cached_database_reconciliation_report(db: Session) -> dict:
    """Bound the cost of an unauthenticated uptime probe.

    Admin reconciliation and the scheduled monitor deliberately call the fresh
    function above. Only the sanitized health endpoint uses this short cache.
    """
    try:
        ttl = min(
            300.0,
            max(5.0, float(os.getenv("AFFILIATE_HEALTH_CACHE_SECONDS", "60") or "60")),
        )
    except ValueError:
        ttl = 60.0
    bind_key = id(db.get_bind())
    now_monotonic = time.monotonic()
    with _health_cache_lock:
        cached = _health_cache.get(bind_key)
        if cached and now_monotonic - cached[0] < ttl:
            return cached[1]
        report = database_reconciliation_report(db)
        if len(_health_cache) >= 16:
            _health_cache.clear()
        _health_cache[bind_key] = (now_monotonic, report)
        return report


def stripe_reconciliation_report(db: Session, stripe_module, *, invoice_limit: int = 100) -> dict:
    """Compare ledger sources and paid payouts with Stripe without mutating data."""
    report = database_reconciliation_report(db)
    issues = list(report["issues"])
    checked_invoices = 0
    checked_transfers = 0
    scanned_paid_invoices = 0
    checked_connect_accounts = 0

    accruals = (
        db.query(AffiliateCommissionEntry)
        .filter(
            AffiliateCommissionEntry.entry_type == "accrual",
            AffiliateCommissionEntry.stripe_invoice_id.isnot(None),
        )
        .order_by(AffiliateCommissionEntry.created_at.desc())
        .limit(invoice_limit)
        .all()
    )
    for entry in accruals:
        try:
            invoice = stripe_module.Invoice.retrieve(entry.stripe_invoice_id)
            invoice = invoice_with_complete_lines(invoice, stripe_module)
            checked_invoices += 1
            currency = str(stripe_field(invoice, "currency") or "").lower()
            commissionable = invoice_commissionable_minor(invoice)
            expected_amount = int(
                (Decimal(commissionable) * Decimal(entry.rate_bps) / Decimal(10_000)).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
            if currency != entry.currency or commissionable != entry.commissionable_minor or expected_amount != entry.amount_minor:
                issues.append(
                    _issue(
                        "critical",
                        "stripe_invoice_mismatch",
                        "Stripe invoice inputs no longer reproduce the stored accrual.",
                        entity_type="commission_entry",
                        entity_id=entry.id,
                        expected={
                            "currency": entry.currency,
                            "commissionable_minor": entry.commissionable_minor,
                            "amount_minor": entry.amount_minor,
                        },
                        actual={
                            "currency": currency,
                            "commissionable_minor": commissionable,
                            "amount_minor": expected_amount,
                        },
                    )
                )
        except Exception as exc:  # Stripe/API availability is itself actionable.
            issues.append(
                _issue(
                    "warning",
                    "stripe_invoice_unreachable",
                    f"Stripe invoice verification failed: {type(exc).__name__}",
                    entity_type="commission_entry",
                    entity_id=entry.id,
                )
            )

    payouts = (
        db.query(AffiliatePayout)
        .filter(
            AffiliatePayout.stripe_transfer_id.isnot(None),
            AffiliatePayout.status.in_(("processing", "paid", "failed")),
        )
        .all()
    )
    for payout in payouts:
        try:
            transfer = stripe_module.Transfer.retrieve(payout.stripe_transfer_id)
            checked_transfers += 1
            actual = {
                "amount_minor": int(stripe_field(transfer, "amount") or 0),
                "currency": str(stripe_field(transfer, "currency") or "").lower(),
                "destination": str(stripe_field(transfer, "destination") or ""),
            }
            partner = db.query(AffiliatePartner).filter(AffiliatePartner.id == payout.partner_id).first()
            expected = {
                "amount_minor": payout.amount_minor,
                "currency": payout.currency,
                "destination": partner.stripe_connect_account_id if partner else None,
            }
            if actual != expected:
                issues.append(
                    _issue(
                        "critical",
                        "stripe_transfer_mismatch",
                        "Stripe transfer does not match the approved payout batch.",
                        entity_type="payout",
                        entity_id=payout.id,
                        expected=expected,
                        actual=actual,
                    )
                )
        except Exception as exc:
            issues.append(
                _issue(
                    "warning",
                    "stripe_transfer_unreachable",
                    f"Stripe transfer verification failed: {type(exc).__name__}",
                    entity_type="payout",
                    entity_id=payout.id,
                )
            )

    connected_partners = (
        db.query(AffiliatePartner)
        .filter(AffiliatePartner.stripe_connect_account_id.isnot(None))
        .all()
    )
    for partner in connected_partners:
        try:
            account = stripe_module.Account.retrieve(partner.stripe_connect_account_id)
            checked_connect_accounts += 1
            stripe_payouts_enabled = bool(stripe_field(account, "payouts_enabled"))
            if partner.payouts_enabled and not stripe_payouts_enabled:
                issues.append(
                    _issue(
                        "critical",
                        "connect_payouts_disabled",
                        "Stripe no longer reports payouts enabled for a payout-ready partner.",
                        entity_type="partner",
                        entity_id=partner.id,
                    )
                )
        except Exception as exc:
            issues.append(
                _issue(
                    "warning",
                    "stripe_connect_account_unreachable",
                    f"Stripe Connect verification failed: {type(exc).__name__}",
                    entity_type="partner",
                    entity_id=partner.id,
                )
            )

    # Known ledger rows only prove what was recorded. Scan Stripe's paid
    # invoices as the independent source so a dropped webhook is visible.
    # The bounded lookback/limit keeps this safe for an operator-triggered run;
    # older windows can be inspected by the single-invoice repair endpoint.
    scan_started_at = utcnow() - timedelta(days=90)
    try:
        page = stripe_module.Invoice.list(
            status="paid",
            created={
                "gte": int(scan_started_at.replace(tzinfo=timezone.utc).timestamp()),
                "lte": int(utcnow().replace(tzinfo=timezone.utc).timestamp()),
            },
            limit=min(100, invoice_limit),
        )
        auto_paging_iter = getattr(page, "auto_paging_iter", None)
        invoices = auto_paging_iter() if callable(auto_paging_iter) else (stripe_field(page, "data") or [])
        for invoice in invoices:
            if scanned_paid_invoices >= invoice_limit:
                break
            scanned_paid_invoices += 1
            invoice_id = str(stripe_field(invoice, "id") or "").strip()
            if not invoice_id:
                continue
            user = user_for_invoice(db, invoice)
            if not user:
                continue
            attribution = (
                db.query(AffiliateAttribution)
                .filter(
                    AffiliateAttribution.invitee_user_id == user.id,
                    AffiliateAttribution.status == "active",
                )
                .first()
            )
            if not attribution:
                continue
            existing = (
                db.query(AffiliateCommissionState.id)
                .filter(AffiliateCommissionState.stripe_invoice_id == invoice_id)
                .first()
            )
            if existing:
                continue
            if affiliate_invoice_decision_exists(db, invoice_id):
                continue
            paid_at_raw = stripe_field(stripe_field(invoice, "status_transitions"), "paid_at")
            paid_at_raw = paid_at_raw or stripe_field(invoice, "created")
            paid_at = datetime.fromtimestamp(int(paid_at_raw or 0), tz=timezone.utc).replace(tzinfo=None)
            if attribution.attributed_at and paid_at < attribution.attributed_at:
                continue
            issues.append(
                _issue(
                    "critical",
                    "paid_invoice_missing_decision",
                    "An attributed paid Stripe invoice has no ledger projection or recorded exclusion decision.",
                    entity_type="stripe_invoice",
                    entity_id=invoice_id,
                )
            )
    except Exception as exc:
        logger.exception("Stripe paid-invoice reconciliation scan failed")
        issues.append(
            _issue(
                "warning",
                "stripe_paid_invoice_scan_unreachable",
                f"Stripe paid-invoice scan failed: {type(exc).__name__}",
            )
        )

    checked = dict(report["checked"])
    checked.update(
        {
            "stripe_invoices": checked_invoices,
            "stripe_transfers": checked_transfers,
            "stripe_connect_accounts": checked_connect_accounts,
            "stripe_paid_invoices_scanned": scanned_paid_invoices,
        }
    )
    return _finish(issues, checked=checked)


def send_monitor_webhook(report: dict) -> bool:
    """Send a sanitized monitor summary when an alert webhook is configured."""
    url = os.getenv("AFFILIATE_ALERT_WEBHOOK_URL", "").strip()
    if not url or report.get("status") == "ok":
        return False
    codes = Counter(issue.get("code") for issue in report.get("issues", []))
    payload = json.dumps(
        {
            "service": "editube-affiliate",
            "status": report.get("status"),
            "generated_at": report.get("generated_at"),
            "issue_counts": report.get("issue_counts"),
            "issue_codes": dict(codes),
        }
    ).encode("utf-8")
    request = Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=10) as response:  # noqa: S310 - explicit operator URL.
            return 200 <= response.status < 300
    except Exception:
        logger.exception("Affiliate monitor webhook delivery failed")
        return False
