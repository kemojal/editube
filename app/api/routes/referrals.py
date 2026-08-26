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

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import AccountCreditLedger, Referral, User
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
    revoke_email_invite,
    send_email_invite,
)
from app.utils.security import get_current_user

router = APIRouter(prefix="/referrals", tags=["Referrals"])
public_router = APIRouter(prefix="/public/referrals", tags=["Referrals"])


class ClaimBody(BaseModel):
    code: str


class InviteBody(BaseModel):
    email: str


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


@public_router.get("/{code}")
def resolve_referral_code(code: str, db: Session = Depends(get_db)):
    """Unauthenticated: is this link good, and whose is it?"""
    return describe_code_for_signup(db, code)
