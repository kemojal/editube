"""
Refer-a-friend endpoints.

``/referrals/me`` is the single read the Settings → Refer & earn panel makes: it
returns the link, the pass count, the program's terms and the list of people who
arrived on it. Terms are served rather than duplicated client-side so the panel
can never promise a number the backend does not pay.

``/public/referrals/{code}`` is the unauthenticated lookup the signup page uses
to show "Kemo invited you". It is deliberately thin — see
``describe_code_for_signup``.
"""

import os
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import (
    AccountCreditLedger,
    Referral,
    ReferralAdminAuditEvent,
    ReferralCode,
    ReferralEmailSuppression,
    ReferralInviteDelivery,
    User,
    UserMFAMethod,
)
from app.services.referrals import (
    MAX_INVITE_SENDS,
    REFERRAL_TERMS,
    ReferralInviteError,
    ReferralRedemptionError,
    build_referral_link,
    credit_balance,
    credits_earned_from_referrals,
    describe_code_for_signup,
    expire_stale_invites,
    get_or_create_referral_code,
    passes_used,
    redeem_referral_code,
    resend_email_invite,
    record_referral_email_event,
    referral_admin_audit,
    revoke_email_invite,
    send_email_invite,
    set_referral_email_suppression,
    clear_referral_email_suppression,
    transfer_credits_to_workspace,
)
from app.utils.security import get_current_user

router = APIRouter(prefix="/referrals", tags=["Referrals"])
public_router = APIRouter(prefix="/public/referrals", tags=["Referrals"])
admin_router = APIRouter(prefix="/admin/referrals", tags=["Referrals-Admin"])


class ClaimBody(BaseModel):
    code: str


class CreditTransferBody(BaseModel):
    workspace_id: int
    amount: int
    idempotency_key: str


class InviteBody(BaseModel):
    email: str


class DeliveryEventBody(BaseModel):
    provider_event_id: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=3, max_length=320)
    event_type: str
    occurred_at: datetime


class AdminCodeBody(BaseModel):
    passes_total: int | None = Field(default=None, ge=0, le=1000)
    revoked: bool | None = None
    reason: str = Field(min_length=20, max_length=1000)

    @model_validator(mode="after")
    def require_change(self):
        if self.passes_total is None and self.revoked is None:
            raise ValueError("Pass capacity or revocation state is required")
        return self


class AdminSuppressionBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    reason: str = Field(min_length=20, max_length=1000)


class AdminClearSuppressionBody(BaseModel):
    reason: str = Field(min_length=20, max_length=1000)


def _require_admin_mfa(db: Session, user: User) -> None:
    if (user.role or "").strip().lower() != "admin":
        raise HTTPException(status_code=403, detail="Referral administration is internal-only")
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
        raise HTTPException(status_code=403, detail="Verified two-factor authentication is required")


def _display_name(user: User | None) -> str | None:
    if not user:
        return None
    name = (user.full_name or user.name or "").strip()
    return name or None


def _referral_payload(db: Session, referral: Referral) -> dict:
    invitee = (
        db.query(User).filter(User.id == referral.invitee_user_id).first()
        if referral.invitee_user_id
        else None
    )
    delivery = (
        db.query(ReferralInviteDelivery)
        .filter(ReferralInviteDelivery.referral_id == referral.id)
        .order_by(ReferralInviteDelivery.attempt_number.desc())
        .first()
    )
    return {
        "id": referral.id,
        "name": _display_name(invitee),
        # The snapshot, not the live account: a deleted account is anonymized,
        # and the referrer should still recognise who they invited.
        "email": referral.invitee_email,
        "status": referral.status,
        "signed_up_at": referral.signed_up_at,
        "converted_at": referral.converted_at,
        "rewarded_at": referral.rewarded_at,
        "reward_credits": referral.reward_credits,
        "invited_at": referral.invited_at,
        "invite_expires_at": referral.invite_expires_at,
        "invite_sends": referral.invite_sends,
        "delivery_status": delivery.status if delivery else None,
        # Computed here so the button's enabled state and the endpoint's rule
        # can't disagree — the panel doesn't get to guess at the send cap.
        "can_resend": referral.status == "invited" and referral.invite_sends < MAX_INVITE_SENDS,
    }


@router.get("/me")
def get_my_referrals(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    code = get_or_create_referral_code(db, current_user)
    # Invites are retired lazily, on the read of the only page that shows them.
    expire_stale_invites(db, current_user.id)

    referrals = (
        db.query(Referral)
        .filter(Referral.referrer_user_id == current_user.id)
        .order_by(Referral.created_at.desc(), Referral.id.desc())
        .all()
    )

    used = passes_used(db, code)

    return {
        "code": code.code,
        "link": build_referral_link(code.code),
        "revoked": code.revoked_at is not None,
        "passes_total": code.passes_total,
        "passes_used": used,
        "passes_left": max(0, code.passes_total - used),
        "terms": REFERRAL_TERMS,
        "credits": {
            "balance": credit_balance(db, current_user.id),
            "earned_from_referrals": credits_earned_from_referrals(db, current_user.id),
        },
        "referrals": [_referral_payload(db, r) for r in referrals],
    }


@router.post("/invites", status_code=201)
def create_email_invite(
    body: InviteBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Email a guest pass to an address.

    Sending reserves one of the referrer's passes, which is what bounds this to
    three addresses rather than three thousand — see
    ``services.referrals.send_email_invite``.
    """
    try:
        referral = send_email_invite(db, current_user, body.email)
    except ReferralInviteError as exc:
        status = 429 if exc.reason == "rate_limited" else 409 if exc.reason in (
            "duplicate",
            "exhausted",
        ) else 400
        raise HTTPException(status_code=status, detail=exc.message) from exc

    return {"invite": _referral_payload(db, referral)}


@router.post("/invites/{referral_id}/resend")
def resend_invite(
    referral_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        referral = resend_email_invite(db, current_user, referral_id)
    except ReferralInviteError as exc:
        status = (
            404
            if exc.reason == "not_found"
            else 429
            if exc.reason == "rate_limited"
            else 409
        )
        raise HTTPException(status_code=status, detail=exc.message) from exc

    return {"invite": _referral_payload(db, referral)}


@router.delete("/invites/{referral_id}", status_code=204)
def revoke_invite(
    referral_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Withdraw an outstanding invite and get the pass back.

    Cannot un-send the mail — the link in it is the shared referral code. What
    it stops is holding a pass for someone who isn't coming.
    """
    try:
        revoke_email_invite(db, current_user, referral_id)
    except ReferralInviteError as exc:
        raise HTTPException(
            status_code=404 if exc.reason == "not_found" else 409, detail=exc.message
        ) from exc
    return None


@router.get("/me/credits")
def get_my_credit_ledger(
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Recent credit movements. Backs the "where did these come from" list."""
    limit = max(1, min(limit, 200))
    entries = (
        db.query(AccountCreditLedger)
        .filter(AccountCreditLedger.user_id == current_user.id)
        .order_by(AccountCreditLedger.created_at.desc(), AccountCreditLedger.id.desc())
        .limit(limit)
        .all()
    )
    return {
        "balance": credit_balance(db, current_user.id),
        "entries": [
            {
                "id": e.id,
                "delta": e.delta,
                "reason": e.reason,
                "description": e.description,
                "created_at": e.created_at,
            }
            for e in entries
        ],
    }


@router.post("/me/credits/transfer")
def transfer_my_credits(
    body: CreditTransferBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        account_balance, workspace_balance = transfer_credits_to_workspace(
            db,
            user=current_user,
            workspace_id=body.workspace_id,
            amount=body.amount,
            idempotency_key=body.idempotency_key,
        )
    except ReferralInviteError as exc:
        status = 403 if exc.reason == "workspace_forbidden" else 409 if exc.reason == "insufficient_credits" else 400
        raise HTTPException(status_code=status, detail=exc.message) from exc
    return {
        "account_balance": account_balance,
        "workspace_id": body.workspace_id,
        "workspace_ugc_balance": workspace_balance,
        "transferred": body.amount,
    }


@router.post("/claim")
def claim_referral_code(
    body: ClaimBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Attach the signed-in account to an invite link.

    Registration handles the email/password path inline; this exists for Google
    SSO, where the account is created by a redirect that never sees the form.
    The frontend holds the code across the round trip and calls this once it is
    back with a session.

    Unlike registration, a failure here is reported: the user asked for the code
    to be applied, so silently dropping it would be a lie.
    """
    if current_user.onboarding_completed:
        # A pass is a welcome gift. Letting an established account claim one
        # turns the program into a coupon anybody can find on the internet.
        raise HTTPException(
            status_code=409,
            detail="Invite links can only be applied to a new account.",
        )

    try:
        referral = redeem_referral_code(db, current_user, body.code)
    except ReferralRedemptionError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc

    if referral is None:
        raise HTTPException(status_code=400, detail="That invite link isn't valid any more.")

    return {
        "applied": True,
        "trial_days": referral.pass_trial_days,
        "signup_credits": REFERRAL_TERMS["invitee_signup_credits"],
    }


@public_router.post("/email-events")
def receive_referral_email_event(
    body: DeliveryEventBody,
    x_referral_webhook_secret: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    expected = os.getenv("REFERRAL_EMAIL_WEBHOOK_SECRET", "")
    if not expected or not x_referral_webhook_secret or not secrets.compare_digest(
        expected,
        x_referral_webhook_secret,
    ):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    occurred_at = body.occurred_at
    if occurred_at.tzinfo is not None:
        occurred_at = occurred_at.astimezone(timezone.utc).replace(tzinfo=None)
    try:
        return record_referral_email_event(
            db,
            provider_event_id=body.provider_event_id,
            email=body.email,
            event_type=body.event_type,
            occurred_at=occurred_at,
        )
    except ReferralInviteError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc


@admin_router.get("/codes")
def admin_list_referral_codes(
    query: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin_mfa(db, current_user)
    rows = db.query(ReferralCode, User).join(User, User.id == ReferralCode.user_id)
    if query:
        needle = f"%{query.strip().lower()}%"
        rows = rows.filter(
            (func.lower(User.email).like(needle))
            | (func.lower(ReferralCode.code).like(needle))
        )
    result = rows.order_by(ReferralCode.created_at.desc()).limit(limit).all()
    return {
        "codes": [
            {
                "id": code.id,
                "user_id": code.user_id,
                "email": user.email,
                "code": code.code,
                "passes_total": code.passes_total,
                "passes_used": passes_used(db, code),
                "revoked_at": code.revoked_at,
                "created_at": code.created_at,
            }
            for code, user in result
        ]
    }


@admin_router.patch("/codes/{code_id}")
def admin_update_referral_code(
    code_id: int,
    body: AdminCodeBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin_mfa(db, current_user)
    code = (
        db.query(ReferralCode)
        .filter(ReferralCode.id == code_id)
        .with_for_update()
        .first()
    )
    if not code:
        raise HTTPException(status_code=404, detail="Referral code not found")
    previous = {"passes_total": code.passes_total, "revoked": code.revoked_at is not None}
    if body.passes_total is not None:
        used = passes_used(db, code)
        if body.passes_total < used:
            raise HTTPException(
                status_code=409,
                detail="Pass capacity cannot be lower than the number already in use",
            )
        code.passes_total = body.passes_total
    if body.revoked is not None:
        code.revoked_at = datetime.utcnow() if body.revoked else None
        if body.revoked:
            pending = (
                db.query(Referral)
                .filter(
                    Referral.referral_code_id == code.id,
                    Referral.status == "invited",
                )
                .all()
            )
            now = datetime.utcnow()
            for referral in pending:
                referral.status = "expired"
                referral.invite_expires_at = now
            if pending:
                db.query(ReferralInviteDelivery).filter(
                    ReferralInviteDelivery.referral_id.in_(
                        [referral.id for referral in pending]
                    ),
                    ReferralInviteDelivery.next_retry_at.isnot(None),
                ).update(
                    {ReferralInviteDelivery.next_retry_at: None},
                    synchronize_session=False,
                )
    referral_admin_audit(
        db,
        "code.updated",
        actor_user_id=current_user.id,
        subject_user_id=code.user_id,
        source_ref=f"referral-code:{code.id}",
        payload={
            "previous": previous,
            "passes_total": code.passes_total,
            "revoked": code.revoked_at is not None,
            "reason": body.reason,
            "pending_invites_expired": len(pending) if body.revoked else 0,
        },
    )
    db.commit()
    db.refresh(code)
    return {
        "code": {
            "id": code.id,
            "user_id": code.user_id,
            "code": code.code,
            "passes_total": code.passes_total,
            "passes_used": passes_used(db, code),
            "revoked_at": code.revoked_at,
        }
    }


@admin_router.get("/deliveries")
def admin_list_referral_deliveries(
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin_mfa(db, current_user)
    query = db.query(ReferralInviteDelivery, Referral).join(
        Referral,
        Referral.id == ReferralInviteDelivery.referral_id,
    )
    if status:
        query = query.filter(ReferralInviteDelivery.status == status)
    rows = query.order_by(ReferralInviteDelivery.created_at.desc()).limit(limit).all()
    return {
        "deliveries": [
            {
                "id": delivery.id,
                "referral_id": referral.id,
                "invitee_email": referral.invitee_email,
                "attempt_number": delivery.attempt_number,
                "retry_count": delivery.retry_count,
                "status": delivery.status,
                "error_code": delivery.error_code,
                "suppression_reason": delivery.suppression_reason,
                "next_retry_at": delivery.next_retry_at,
                "last_attempt_at": delivery.last_attempt_at,
                "created_at": delivery.created_at,
            }
            for delivery, referral in rows
        ]
    }


@admin_router.get("/suppressions")
def admin_list_referral_suppressions(
    active_only: bool = Query(default=True),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin_mfa(db, current_user)
    query = db.query(ReferralEmailSuppression)
    if active_only:
        query = query.filter(ReferralEmailSuppression.cleared_at.is_(None))
    rows = query.order_by(ReferralEmailSuppression.suppressed_at.desc()).limit(limit).all()
    return {
        "suppressions": [
            {
                "id": item.id,
                "email_hash": item.email_hash,
                "reason": item.reason,
                "source": item.source,
                "suppressed_at": item.suppressed_at,
                "cleared_at": item.cleared_at,
            }
            for item in rows
        ]
    }


@admin_router.post("/suppressions", status_code=201)
def admin_create_referral_suppression(
    body: AdminSuppressionBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin_mfa(db, current_user)
    try:
        suppression = set_referral_email_suppression(
            db,
            email=body.email,
            reason=body.reason,
            admin=current_user,
        )
    except ReferralInviteError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    return {"suppression_id": suppression.id, "email_hash": suppression.email_hash}


@admin_router.post("/suppressions/{suppression_id}/clear")
def admin_clear_referral_suppression(
    suppression_id: int,
    body: AdminClearSuppressionBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin_mfa(db, current_user)
    suppression = (
        db.query(ReferralEmailSuppression)
        .filter(ReferralEmailSuppression.id == suppression_id)
        .first()
    )
    if not suppression:
        raise HTTPException(status_code=404, detail="Suppression not found")
    try:
        clear_referral_email_suppression(
            db,
            suppression=suppression,
            admin=current_user,
            reason=body.reason,
        )
    except ReferralInviteError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    return {"cleared": True}


@admin_router.get("/audit")
def admin_list_referral_audit(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin_mfa(db, current_user)
    events = (
        db.query(ReferralAdminAuditEvent)
        .order_by(ReferralAdminAuditEvent.created_at.desc())
        .limit(limit)
        .all()
    )
    return {
        "events": [
            {
                "id": item.id,
                "event_type": item.event_type,
                "actor_user_id": item.actor_user_id,
                "subject_user_id": item.subject_user_id,
                "source_ref": item.source_ref,
                "payload": item.payload,
                "created_at": item.created_at,
            }
            for item in events
        ]
    }


@public_router.get("/{code}")
def resolve_referral_code(code: str, db: Session = Depends(get_db)):
    """Unauthenticated: is this link good, and whose is it?"""
    return describe_code_for_signup(db, code)
