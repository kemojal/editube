"""
Refer-a-friend: guest passes in, AI credits out.

The program has two sides and they are deliberately different currencies:

* the **friend** gets a *guest pass* — an extended trial (``PASS_TRIAL_DAYS``
  instead of the standing 14 days everyone gets) plus a starting credit balance.
  A pass has to beat the default trial or it is not a gift, it is a rename.
* the **referrer** gets AI credits, and only once the friend actually pays.
  Rewarding a signup would pay for email addresses; rewarding a trial would pay
  for a card that never charges.

Every number the program promises lives in :data:`REFERRAL_TERMS` and is served
to the client, so the settings panel, the signup banner and the emails cannot
drift apart or quietly disagree with what was paid.

This is separate from the *affiliate* program (cash commission on a share of
revenue, application-gated, ``/partners/affiliate``). Same instinct, different
audience: this one is for a user handing the product to a friend.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
from datetime import datetime, timedelta

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import AccountCreditLedger, Referral, ReferralCode, User

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Terms
# ---------------------------------------------------------------------------

#: Guest passes a new code is issued with. Scarcity is the point: an unlimited
#: link gets posted to a deal aggregator, three passes get sent to three people
#: who might actually edit video.
DEFAULT_PASSES = 3

#: The friend's trial, in days. Must stay ahead of billing.TRIAL_DAYS (14) or the
#: pass is worth nothing.
PASS_TRIAL_DAYS = 30

#: Credits the friend starts with, so the longer trial has something to spend.
INVITEE_SIGNUP_CREDITS = 200

#: Credits the referrer earns per friend who converts to a paid subscription.
REFERRER_REWARD_CREDITS = 500

#: How long an emailed invite stays live before the pass is handed back.
INVITE_EXPIRY_DAYS = 14

#: Mails per invite: the first one, plus a single nudge. Two is a favour; five
#: is spam, and it is our sending reputation that pays for it.
MAX_INVITE_SENDS = 2

#: Ceiling on invite mails per referrer per day, independent of passes. Passes
#: alone don't bound this — revoke-and-resend would otherwise loop forever.
MAX_INVITE_SENDS_PER_DAY = 5

#: Alphabet without O/0/I/1/L — codes get read down a phone line.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 8

#: Deliberately permissive. This is a "did they fat-finger it" check, not an
#: attempt to decide what RFC 5321 allows; the send result is the real verdict.
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

REFERRAL_TERMS = {
    "passes_per_user": DEFAULT_PASSES,
    "pass_trial_days": PASS_TRIAL_DAYS,
    "invitee_signup_credits": INVITEE_SIGNUP_CREDITS,
    "referrer_reward_credits": REFERRER_REWARD_CREDITS,
    "invite_expiry_days": INVITE_EXPIRY_DAYS,
    "max_invite_sends": MAX_INVITE_SENDS,
    #: What the friend has to do before the referrer is paid. Stated once here
    #: so the panel's fine print is generated, not written by hand.
    "reward_trigger": "subscribed",
}

#: Statuses that have consumed a guest pass. An outstanding invite holds one —
#: that is what caps outbound mail at three addresses. Voided and expired
#: referrals hand the pass back; the referrer should not be charged for someone
#: else's abuse, or for a friend who never replied.
_PASS_CONSUMING_STATUSES = ("invited", "signed_up", "trialing", "rewarded")


def _frontend_base() -> str:
    return os.getenv("FRONTEND_BASE_URL", "http://localhost:3000").rstrip("/")


def build_referral_link(code: str) -> str:
    """The URL a user copies. Points at signup, which already knows ``?ref=``."""
    return f"{_frontend_base()}/signup?ref={code}"


# ---------------------------------------------------------------------------
# Codes
# ---------------------------------------------------------------------------


def _generate_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


def get_or_create_referral_code(db: Session, user: User) -> ReferralCode:
    """
    The user's share link, minted on first look.

    Codes are created lazily rather than at signup so the 30k accounts that
    predate the program do not need a backfill, and so a code only exists for
    someone who has actually opened the panel.
    """
    existing = db.query(ReferralCode).filter(ReferralCode.user_id == user.id).first()
    if existing:
        return existing

    # A collision on an 8-char / 31-symbol code is ~1 in 8.5e11, but the unique
    # index is the thing that actually guarantees it, so retry on its say-so.
    for _ in range(5):
        candidate = ReferralCode(
            user_id=user.id, code=_generate_code(), passes_total=DEFAULT_PASSES
        )
        db.add(candidate)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            # Another request may have created this user's row concurrently.
            concurrent = db.query(ReferralCode).filter(ReferralCode.user_id == user.id).first()
            if concurrent:
                return concurrent
            continue
        db.refresh(candidate)
        return candidate

    raise RuntimeError("Could not allocate a referral code")


def find_code(db: Session, code: str) -> ReferralCode | None:
    """
    Look up a code as typed — tolerant of case, spaces and the hyphens people
    add when they retype one from a screenshot.

    Compares against the stored value directly rather than ``upper(code)`` so
    the unique index is used; codes are only ever minted uppercase.
    """
    normalized = (code or "").strip().replace(" ", "").replace("-", "").upper()
    if not normalized:
        return None
    return db.query(ReferralCode).filter(ReferralCode.code == normalized).first()


def passes_used(db: Session, referral_code: ReferralCode) -> int:
    """
    Passes currently spoken for.

    An invite that has run past its expiry is discounted here even if
    :func:`expire_stale_invites` has not swept it yet, so the count is right the
    moment it is true rather than the moment a write happens to run.
    """
    now = datetime.utcnow()
    return (
        db.query(func.count(Referral.id))
        .filter(
            Referral.referral_code_id == referral_code.id,
            Referral.status.in_(_PASS_CONSUMING_STATUSES),
            or_(
                Referral.status != "invited",
                Referral.invite_expires_at.is_(None),
                Referral.invite_expires_at > now,
            ),
        )
        .scalar()
        or 0
    )


def expire_stale_invites(db: Session, referrer_user_id: int) -> int:
    """
    Retire invites nobody acted on, handing their passes back.

    Swept lazily on read rather than by a cron job: the only person who cares is
    the referrer, and they find out by opening the panel — which is exactly when
    this runs. Returns how many were expired.
    """
    now = datetime.utcnow()
    stale = (
        db.query(Referral)
        .filter(
            Referral.referrer_user_id == referrer_user_id,
            Referral.status == "invited",
            Referral.invite_expires_at.isnot(None),
            Referral.invite_expires_at <= now,
        )
        .all()
    )
    for referral in stale:
        referral.status = "expired"
    if stale:
        db.commit()
    return len(stale)


def passes_left(db: Session, referral_code: ReferralCode) -> int:
    return max(0, referral_code.passes_total - passes_used(db, referral_code))


# ---------------------------------------------------------------------------
# Credits
# ---------------------------------------------------------------------------


def credit_balance(db: Session, user_id: int) -> int:
    return (
        db.query(func.coalesce(func.sum(AccountCreditLedger.delta), 0))
        .filter(AccountCreditLedger.user_id == user_id)
        .scalar()
        or 0
    )


def credits_earned_from_referrals(db: Session, user_id: int) -> int:
    return (
        db.query(func.coalesce(func.sum(AccountCreditLedger.delta), 0))
        .filter(
            AccountCreditLedger.user_id == user_id,
            AccountCreditLedger.reason.in_(("referral_reward", "referral_signup_bonus")),
        )
        .scalar()
        or 0
    )


def grant_credits(
    db: Session,
    *,
    user_id: int,
    delta: int,
    reason: str,
    source_ref: str | None,
    description: str | None = None,
) -> AccountCreditLedger | None:
    """
    Append one ledger entry, at most once per ``(user, reason, source_ref)``.

    Returns ``None`` when the grant was already made — Stripe retries webhooks,
    and paying twice for one conversion is the expensive kind of bug. The caller
    is expected to commit.

    The insert runs inside a SAVEPOINT so that losing the uniqueness race
    discards *only* this entry. A plain rollback here would also throw away the
    referral-status changes the caller made in the same transaction, leaving a
    referral that converted but never records it.
    """
    entry = AccountCreditLedger(
        user_id=user_id,
        delta=delta,
        reason=reason,
        source_ref=source_ref,
        description=description,
    )
    try:
        with db.begin_nested():
            db.add(entry)
    except IntegrityError:
        logger.info(
            "credit grant already applied user=%s reason=%s ref=%s", user_id, reason, source_ref
        )
        return None
    return entry


# ---------------------------------------------------------------------------
# Redemption
# ---------------------------------------------------------------------------


class ReferralRedemptionError(Exception):
    """A code was offered but cannot be redeemed. Carries a user-facing reason."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


def describe_code_for_signup(db: Session, code: str) -> dict:
    """
    Public view of a code, for the banner on the signup page.

    Deliberately leaks nothing but a first name: a code is a semi-public string
    and this endpoint is unauthenticated, so it must not confirm an email
    address or expose how many people someone has referred.
    """
    referral_code = find_code(db, code)
    if not referral_code or referral_code.revoked_at is not None:
        return {"valid": False, "reason": "unknown"}

    if passes_left(db, referral_code) <= 0:
        return {"valid": False, "reason": "exhausted"}

    referrer = db.query(User).filter(User.id == referral_code.user_id).first()
    if not referrer or referrer.deleted_at is not None:
        return {"valid": False, "reason": "unknown"}

    display = (referrer.full_name or referrer.name or "").strip()
    first_name = display.split(" ")[0] if display else None

    return {
        "valid": True,
        "code": referral_code.code,
        "referrer_first_name": first_name,
        "trial_days": PASS_TRIAL_DAYS,
        "signup_credits": INVITEE_SIGNUP_CREDITS,
    }


def redeem_referral_code(db: Session, invitee: User, code: str) -> Referral | None:
    """
    Attach a freshly created account to the link it arrived on.

    Safe to call more than once for the same account — the second call returns
    the existing referral rather than burning another pass — because signup and
    the post-SSO claim can both reach it for one user.

    Raises :class:`ReferralRedemptionError` when the code cannot be honoured, so
    the caller can decide whether that is fatal (an explicit "apply this code"
    action) or merely worth ignoring (registration, which must not fail because
    a stale link was in the URL).
    """
    already = db.query(Referral).filter(Referral.invitee_user_id == invitee.id).first()
    if already:
        return already

    referral_code = find_code(db, code)
    if not referral_code or referral_code.revoked_at is not None:
        raise ReferralRedemptionError("unknown", "That invite link isn't valid any more.")

    if referral_code.user_id == invitee.id:
        raise ReferralRedemptionError("self_referral", "You can't invite yourself.")

    if passes_left(db, referral_code) <= 0:
        raise ReferralRedemptionError(
            "exhausted", "This invite link has already been used up."
        )

    referral = Referral(
        referrer_user_id=referral_code.user_id,
        referral_code_id=referral_code.id,
        code=referral_code.code,
        invitee_user_id=invitee.id,
        invitee_email=invitee.email,
        status="signed_up",
        pass_trial_days=PASS_TRIAL_DAYS,
    )
    db.add(referral)
    try:
        db.flush()
    except IntegrityError:
        # Lost a race against a concurrent signup path for the same account.
        db.rollback()
        return db.query(Referral).filter(Referral.invitee_user_id == invitee.id).first()

    grant_credits(
        db,
        user_id=invitee.id,
        delta=INVITEE_SIGNUP_CREDITS,
        reason="referral_signup_bonus",
        source_ref=f"referral:{referral.id}",
        description="Welcome credits from an invite",
    )
    db.commit()
    db.refresh(referral)
    return referral


def active_referral_for_invitee(db: Session, user_id: int) -> Referral | None:
    """The referral a user arrived on, if it is still live."""
    return (
        db.query(Referral)
        .filter(Referral.invitee_user_id == user_id, Referral.status != "void")
        .first()
    )


def trial_days_for_user(db: Session, user_id: int, default_days: int) -> int:
    """
    Trial length at checkout: the guest pass if one is unspent, else the standard.

    The pass is marked spent here rather than at signup, because that is the
    moment it actually buys something.
    """
    referral = active_referral_for_invitee(db, user_id)
    if not referral or referral.pass_redeemed_at is not None:
        return default_days
    return max(default_days, referral.pass_trial_days)


def mark_pass_redeemed(db: Session, user_id: int) -> None:
    referral = active_referral_for_invitee(db, user_id)
    if referral and referral.pass_redeemed_at is None:
        referral.pass_redeemed_at = datetime.utcnow()


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


def sync_referral_from_subscription(db: Session, user: User, status: str | None, plan: str | None) -> None:
    """
    Move a referral along as the friend's subscription changes, and pay out.

    Called from the Stripe webhook path for whoever the subscription belongs to;
    a no-op for the overwhelming majority of users, who were never referred.
    Never raises: a referral bookkeeping problem must not fail a webhook and
    cause Stripe to retry a subscription sync that already succeeded.
    """
    try:
        referral = active_referral_for_invitee(db, user.id)
        if not referral or referral.status == "rewarded":
            return

        paid_plan = (plan or user.plan or "").lower() in ("pro", "scale", "enterprise")

        if status == "trialing" and referral.status == "signed_up":
            referral.status = "trialing"
            db.commit()
            return

        if status != "active" or not paid_plan:
            return

        if referral.converted_at is None:
            referral.converted_at = datetime.utcnow()

        entry = grant_credits(
            db,
            user_id=referral.referrer_user_id,
            delta=REFERRER_REWARD_CREDITS,
            reason="referral_reward",
            source_ref=f"referral:{referral.id}",
            description="A friend you invited subscribed",
        )
        # `None` means the grant already exists from an earlier delivery of this
        # event; the referral should still settle into its final state.
        referral.status = "rewarded"
        referral.rewarded_at = referral.rewarded_at or datetime.utcnow()
        referral.reward_credits = REFERRER_REWARD_CREDITS
        db.commit()
        if entry is not None:
            logger.info(
                "referral rewarded referral=%s referrer=%s credits=%s",
                referral.id,
                referral.referrer_user_id,
                REFERRER_REWARD_CREDITS,
            )
    except Exception:  # pragma: no cover - defensive; webhook must still 200
        db.rollback()
        logger.exception("referral sync failed for user=%s", getattr(user, "id", None))
