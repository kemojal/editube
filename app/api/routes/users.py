from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Query
from fastapi.responses import RedirectResponse
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from pydantic import BaseModel
from datetime import datetime
import logging
import os
import secrets
from urllib.parse import quote as url_quote

from ..models.users import User as UserSchema, UserCreate, UserUpdate, UserRegisterSchema, UserLoginSchema, OnboardingProfileUpdate, OnboardingWorkflowUpdate, OnboardingPlanUpdate, UserSettingsResponse, UserSettingsUpdate, ApiTokenResponse, ApiTokenCreateRequest, ApiTokenCreateResponse
from ...db.database import get_db
from app.db.models import (
    ApiToken,
    User,
    UserCaptionFavorite,
    UserSettings,
    UserSession,
    UserMFAMethod,
    UserMFARecoveryCode,
    Workspace,
    WorkspaceSSOProvider,
    WorkspaceMember,
    WorkspaceAuthPolicy,
)
from app.api.models.users import UserResponse
from ...utils.security import (
    get_password_hash,
    verify_password,
    get_current_user,
    create_access_token,
    create_refresh_token,
    verify_refresh_token,
    create_user_session,
    validate_refresh_session,
    decode_access_token_payload,
    revoke_user_session,
    hash_api_token,
    API_TOKEN_PREFIX,
)
from app.utils.cloudinary import upload_file_to_cloudinary
from app.services.mfa_totp import (
    TOTP_ISSUER,
    build_otpauth_url,
    clear_mfa_attempts,
    generate_recovery_codes,
    generate_totp_secret,
    hash_recovery_codes,
    mfa_attempts_remaining,
    record_failed_mfa_attempt,
    verify_recovery_code,
    verify_totp_code,
)
from app.services.security_audit import log_security_audit_event
from app.services.oidc_sso import (
    build_oidc_authorize_url,
    build_signed_sso_state,
    verify_signed_sso_state,
    exchange_oidc_code,
    fetch_oidc_userinfo,
    validate_oidc_claims,
)
from app.services.pricing import normalize_plan_key

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/users",
    tags=["Users"],
)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


DEFAULT_USER_SETTINGS = {
    "workspace_name": "My Workspace",
    "timezone": "America/Los_Angeles",
    "theme": "system",
    "date_format": "MMM d, yyyy",
    "email_comments": True,
    "email_mentions": True,
    "product_updates": False,
    "two_factor": False,
    "session_timeout": "30",
    "allow_project_invites": True,
    "email_mention_digest": "off",
    "share_data": False,
    "default_publish_privacy": "private",
    "ai_model_preferences": {},
}
ALLOWED_TIMEZONES = {
    "America/Los_Angeles",
    "America/New_York",
    "Europe/London",
    "Asia/Singapore",
}
ALLOWED_DATE_FORMATS = {"MMM d, yyyy", "yyyy-MM-dd", "MM/dd/yyyy"}

#: Lifetime of the token handed out between password and second factor. It is
#: not a session, so it should not live as long as one — long enough to fetch a
#: code from a phone, short enough that a leaked challenge token is worthless.
MFA_CHALLENGE_TTL_MINUTES = 10


def get_or_create_user_settings(db: Session, user_id: int) -> UserSettings:
    settings = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
    if settings:
        return settings

    settings = UserSettings(user_id=user_id, **DEFAULT_USER_SETTINGS)
    db.add(settings)
    db.commit()
    db.refresh(settings)
    return settings


# ---------------------------------------------------------------------------
# AI model catalog (choices surfaced in Settings → AI models)
# ---------------------------------------------------------------------------


def _pretty_model_label(model_id: str) -> str:
    name = model_id.split("/")[-1].replace("-", " ").replace("_", " ")
    return " ".join(word.capitalize() for word in name.split())


#: Human copy for generation models. Ids not listed here still appear — they
#: just fall back to the prettified id with no supporting line.
_GENERATION_MODEL_NOTES: dict[str, str] = {
    "veo-3.1-generate-preview": "Highest quality, native audio",
    "veo-3.1-lite-generate-preview": "Faster and cheaper, 720p",
    "gemini-omni-flash-video": "Fastest, up to 10s",
    "gemini-3.1-flash-image-preview": "Fast and economical",
    "gemini-3-pro-image": "Highest quality",
    "google/gemini-3.1-flash-image": "Fast, via OpenRouter",
    "google/gemini-3.1-flash-lite-image": "Cheapest, via OpenRouter",
    "google/gemini-3-pro-image-preview": "Highest quality, via OpenRouter",
    "openai/gpt-5-image": "Strong prompt adherence",
    "openai/gpt-5-image-mini": "Fast and cheap",
    "seedance-2.0": "Up to 4K with audio",
    "seedance-2.0-mini": "Fast 720p drafts",
    "seedance-2.0-fast": "Lowest latency Seedance",
}


def _build_ai_model_catalog() -> dict:
    # Image/video options are derived from the generation registry so this stays
    # in sync with what the media pipeline can actually run. Models the registry
    # knows about but cannot execute yet are listed as unavailable rather than
    # hidden, so the picker shows the roadmap without handing out a 500.
    from app.api.routes.ai_media import IMPLEMENTED_MODELS, PLANNED_MODELS
    from app.services.transcription_models import (
        default_transcription_model_id,
        transcription_catalog_payload,
    )

    image_models, video_models = [], []
    for model_id, provider in {**IMPLEMENTED_MODELS, **PLANNED_MODELS}.items():
        entry = {
            "id": model_id,
            "label": _pretty_model_label(model_id),
            "provider": provider,
            "available": model_id in IMPLEMENTED_MODELS,
        }
        note = _GENERATION_MODEL_NOTES.get(model_id)
        if note:
            entry["note"] = note
        (image_models if "image" in model_id else video_models).append(entry)

    editing = [
        {"id": "gemini-3-flash-preview", "label": "Gemini 3 Flash", "provider": "gemini", "note": "Fast and economical", "available": True},
        {"id": "gemini-3-pro", "label": "Gemini 3 Pro", "provider": "gemini", "note": "Highest quality", "available": True},
    ]

    return {
        "categories": [
            {
                "key": "transcription",
                "label": "Transcription",
                "description": "Model used to transcribe your videos.",
                "models": transcription_catalog_payload(),
                "default": default_transcription_model_id(),
                # Rendered as scrollable cards with accuracy/speed meters rather
                # than a plain dropdown — there are 16 of these and the choice
                # turns on size and language coverage, not just the name.
                "picker": "cards",
            },
            {
                "key": "editing",
                "label": "Review & editing",
                "description": "Model used to review and edit transcripts in the editor.",
                "models": editing,
                "default": os.getenv("GEMINI_MODEL", "gemini-3-flash-preview"),
            },
            {
                "key": "image",
                "label": "Image generation",
                "description": "Default model for AI image generation.",
                "models": image_models,
                "default": "gemini-3.1-flash-image-preview",
            },
            {
                "key": "video",
                "label": "Video generation",
                "description": "Default model for AI video generation.",
                "models": video_models,
                "default": "veo-3.1-generate-preview",
            },
        ]
    }


@router.get("/ai/model-catalog")
def get_ai_model_catalog(current_user: User = Depends(get_current_user)):
    """Available AI models per capability, for the Settings → AI models picker."""
    return _build_ai_model_catalog()


# ---------------------------------------------------------------------------
# Personal API tokens
# ---------------------------------------------------------------------------


@router.get("/me/api-tokens", response_model=list[ApiTokenResponse])
def list_api_tokens(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return (
        db.query(ApiToken)
        .filter(ApiToken.user_id == current_user.id)
        .order_by(ApiToken.created_at.desc())
        .all()
    )


@router.post("/me/api-tokens", response_model=ApiTokenCreateResponse, status_code=201)
def create_api_token(
    payload: ApiTokenCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="Token name is required")
    if len(name) > 120:
        name = name[:120]

    # Generate a token that cannot collide; hash is unique-indexed so retry defensively.
    for _ in range(5):
        raw_token = f"{API_TOKEN_PREFIX}{secrets.token_hex(20)}"
        token_hash = hash_api_token(raw_token)
        if not db.query(ApiToken).filter(ApiToken.token_hash == token_hash).first():
            break
    else:  # pragma: no cover - astronomically unlikely
        raise HTTPException(status_code=500, detail="Could not generate a unique token")

    record = ApiToken(
        user_id=current_user.id,
        name=name,
        token_prefix=raw_token[:12],
        token_hash=token_hash,
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    # `token` is included only on this creation response and never persisted in clear.
    return ApiTokenCreateResponse(
        id=record.id,
        name=record.name,
        token_prefix=record.token_prefix,
        last_used_at=record.last_used_at,
        created_at=record.created_at,
        token=raw_token,
    )


@router.delete("/me/api-tokens/{token_id}", status_code=204)
def revoke_api_token(
    token_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    record = (
        db.query(ApiToken)
        .filter(ApiToken.id == token_id, ApiToken.user_id == current_user.id)
        .first()
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Token not found")
    db.delete(record)
    db.commit()


@router.delete("/me", status_code=204)
def delete_my_account(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Deactivate the current account.

    A hard delete is unsafe here: many rows (projects, media, workspace content)
    reference ``users.id`` without ``ON DELETE CASCADE``, and cascading would
    destroy content shared with a workspace. Instead we soft-delete: revoke all
    sessions/tokens, anonymize personal data, and mark the row deleted so auth
    rejects it. Owned content remains under an anonymized tombstone user.
    """
    user = current_user
    now = datetime.utcnow()

    # Best-effort: cancel any active Stripe subscription so a deleted account
    # is not billed further. Webhook/portal reconciliation is the backstop.
    if user.stripe_subscription_id:
        try:
            import stripe

            secret = os.getenv("STRIPE_SECRET_KEY")
            if secret:
                stripe.api_key = secret
                try:
                    stripe.Subscription.cancel(user.stripe_subscription_id)
                except AttributeError:  # older stripe SDKs
                    stripe.Subscription.delete(user.stripe_subscription_id)
        except Exception:
            pass

    # Revoke every session and personal access token.
    db.query(UserSession).filter(UserSession.user_id == user.id).update(
        {"revoked": True, "revoked_at": now}, synchronize_session=False
    )
    db.query(ApiToken).filter(ApiToken.user_id == user.id).delete(synchronize_session=False)

    # Anonymize personal data (keep the row so owned content stays intact).
    user.deleted_at = now
    user.email = f"deleted+{user.id}@deleted.local"
    user.name = "Deleted user"
    user.full_name = None
    user.avatar_url = None
    user.phone = None
    user.hashed_password = None
    user.google_sub = None
    user.mfa_required = False
    user.subscription_status = "canceled"

    db.commit()


# @router.post("/register")
# def register_user(user_data: UserRegisterSchema, db: Session = Depends(get_db)):
#     # Implement user registration logic here

#     return [{"username": "Rick"}, {"username": "Morty"}]

#     pass

# @router.post("/login")
# def login_user(user_data: UserLoginSchema, db: Session = Depends(get_db)):
#     # Implement user login logic here
#     pass

# # Add other user-related routes as needed
# @router.post("/register", response_model=UserRegisterSchema)
@router.post("/register")
def register_user(user: UserCreate, db: Session = Depends(get_db)):
    existing_user = db.query(User).filter(User.email == user.email).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered")

    hashed_password = get_password_hash(user.password)
    db_user = User(email=user.email, hashed_password=hashed_password, name=user.name, role=user.role)
    db.add(db_user)
    db.commit()
    db.refresh(db_user)

    from app.services.workspace_bootstrap import ensure_personal_workspace

    ensure_personal_workspace(db, db_user)

    # An invite link is a nice-to-have on top of an account that already exists
    # by this point. A code that is stale, exhausted or self-referring must
    # leave the signup itself untouched — the panel and the claim endpoint are
    # where a user is told a code did not take.
    if user.referral_code:
        from app.services.referrals import ReferralRedemptionError, redeem_referral_code

        try:
            redeem_referral_code(db, db_user, user.referral_code)
        except ReferralRedemptionError:
            pass
        except Exception:
            logger.exception(
                "referral redemption failed during registration user=%s", db_user.id
            )

    session_id = create_user_session(db, db_user.id)
    access_token = create_access_token(
        data={"user_id": db_user.id, "onboarding_completed": False, "sid": session_id}
    )
    refresh_token = create_refresh_token(data={"user_id": db_user.id, "sid": session_id})
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "refresh_token": refresh_token,
        "onboarding_completed": False,
    }

@router.post("/login")
def login_user(user_credentials: UserLoginSchema, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == user_credentials.email).first()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    
    if not user.hashed_password:
        raise HTTPException(
            status_code=401,
            detail="This account uses Google sign-in. Continue with Google.",
        )

    if not verify_password(user_credentials.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    
    mfa_method = _mfa_method(db, user.id, verified_only=True)
    settings = get_or_create_user_settings(db, user.id)
    # A verified authenticator gates sign-in on its own. The two flags are only
    # consulted as a backstop, and a flag set without an enrolled method can no
    # longer strand the account: there is nothing to prove, so let them in and
    # let the Security panel put it right.
    if mfa_method:
        challenge_token = create_access_token(
            data={
                "user_id": user.id,
                "onboarding_completed": user.onboarding_completed,
                "mfa_pending": True,
            },
            expires_minutes=MFA_CHALLENGE_TTL_MINUTES,
        )
        log_security_audit_event(
            db,
            action="auth.login.primary_passed_mfa_required",
            resource_type="user",
            resource_id=str(user.id),
            actor_user_id=user.id,
            actor_type="user",
        )
        db.commit()
        return {"mfa_required": True, "challenge_token": challenge_token}

    # Flag set with nothing enrolled — the old code raised a 400 here, which
    # locked the account out permanently with no way to reach the settings that
    # would fix it. Clear the stale flag and let them in.
    if user.mfa_required or bool(getattr(settings, "two_factor", False)):
        user.mfa_required = False
        settings.two_factor = False
        db.commit()

    session_id = create_user_session(db, user.id)
    access_token = create_access_token(
        data={
            "user_id": user.id,
            "onboarding_completed": user.onboarding_completed,
            "sid": session_id,
        }
    )
    refresh_token = create_refresh_token(data={"user_id": user.id, "sid": session_id})
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "refresh_token": refresh_token,
        "onboarding_completed": user.onboarding_completed,
    }


RECOVERY_CODE_COUNT = 10


def _mfa_method(db: Session, user_id: int, *, verified_only: bool = False) -> UserMFAMethod | None:
    query = db.query(UserMFAMethod).filter(
        UserMFAMethod.user_id == user_id,
        UserMFAMethod.disabled_at.is_(None),
    )
    if verified_only:
        query = query.filter(UserMFAMethod.verified_at.isnot(None))
    return query.order_by(UserMFAMethod.id.desc()).first()


def _unused_recovery_code_count(db: Session, user_id: int) -> int:
    return (
        db.query(UserMFARecoveryCode)
        .filter(
            UserMFARecoveryCode.user_id == user_id,
            UserMFARecoveryCode.used_at.is_(None),
        )
        .count()
    )


def _issue_recovery_codes(db: Session, user_id: int) -> list[str]:
    """Replace the user's recovery codes and return the new plaintext set.

    The plaintext is returned exactly once — only hashes are stored, so a lost
    set can be regenerated but never re-read.
    """
    raw_codes = generate_recovery_codes(RECOVERY_CODE_COUNT)
    db.query(UserMFARecoveryCode).filter(UserMFARecoveryCode.user_id == user_id).delete(
        synchronize_session=False
    )
    for hashed in hash_recovery_codes(raw_codes):
        db.add(UserMFARecoveryCode(user_id=user_id, code_hash=hashed))
    return raw_codes


def _workspace_requires_mfa(db: Session, user_id: int) -> bool:
    """True when a workspace the user belongs to mandates a second factor.

    Such a user must not be able to turn their own authenticator off — they
    would be locked out at the next sign-in with no way back in.
    """
    return (
        db.query(WorkspaceAuthPolicy)
        .join(
            WorkspaceMember,
            WorkspaceMember.workspace_id == WorkspaceAuthPolicy.workspace_id,
        )
        .filter(
            WorkspaceMember.user_id == user_id,
            WorkspaceAuthPolicy.mfa_required.is_(True),
        )
        .first()
        is not None
    )


def _disable_mfa_rows(db: Session, user: User) -> None:
    now = datetime.utcnow()
    db.query(UserMFAMethod).filter(
        UserMFAMethod.user_id == user.id,
        UserMFAMethod.disabled_at.is_(None),
    ).update({"disabled_at": now}, synchronize_session=False)
    db.query(UserMFARecoveryCode).filter(UserMFARecoveryCode.user_id == user.id).delete(
        synchronize_session=False
    )
    user.mfa_required = False
    settings = get_or_create_user_settings(db, user.id)
    settings.two_factor = False


def _verify_second_factor(
    db: Session,
    user: User,
    *,
    code: str = "",
    recovery_code: str = "",
) -> bool:
    """Check a TOTP code or a recovery code against the user's active method.

    A matched recovery code is burned here — single use is the whole point of
    the list.
    """
    method = _mfa_method(db, user.id, verified_only=True)
    if not method:
        return False
    if code:
        return verify_totp_code(method.secret_encrypted, code)
    if recovery_code:
        rows = (
            db.query(UserMFARecoveryCode)
            .filter(
                UserMFARecoveryCode.user_id == user.id,
                UserMFARecoveryCode.used_at.is_(None),
            )
            .all()
        )
        matched_hash = verify_recovery_code(recovery_code, [row.code_hash for row in rows])
        if not matched_hash:
            return False
        for row in rows:
            if row.code_hash == matched_hash:
                row.used_at = datetime.utcnow()
                break
        return True
    return False


def _guard_mfa_attempts(db: Session, user_id: int, scope: str) -> None:
    """Refuse further attempts once the per-user budget is spent."""
    if mfa_attempts_remaining(f"{scope}:{user_id}") > 0:
        return
    log_security_audit_event(
        db,
        action="auth.mfa.rate_limited",
        resource_type="user",
        resource_id=str(user_id),
        actor_user_id=user_id,
        actor_type="user",
        outcome="denied",
        metadata={"scope": scope},
    )
    db.commit()
    raise HTTPException(
        status_code=429,
        detail="Too many incorrect codes. Wait a few minutes and try again.",
    )


@router.get("/me/mfa")
def get_mfa_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """What the Security panel renders: is a second factor active, and how many
    recovery codes are left."""
    method = _mfa_method(db, current_user.id)
    verified = bool(method and method.verified_at)
    return {
        "enabled": verified,
        "pending_setup": bool(method and not method.verified_at),
        "method_type": method.method_type if method else None,
        "label": method.label if method else None,
        "verified_at": method.verified_at.isoformat() if method and method.verified_at else None,
        "created_at": method.created_at.isoformat() if method and method.created_at else None,
        "recovery_codes_remaining": _unused_recovery_code_count(db, current_user.id) if verified else 0,
        "recovery_codes_total": RECOVERY_CODE_COUNT,
        # The switch is rendered locked when a workspace policy mandates MFA.
        "enforced_by_workspace": _workspace_requires_mfa(db, current_user.id),
    }


@router.post("/mfa/enroll")
def enroll_mfa(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Start (or restart) authenticator setup and hand back the QR payload.

    Refuses while a verified method is active. The previous version overwrote
    the live secret on every call, so re-opening the setup dialog silently
    invalidated the authenticator the user was already relying on. Recovery
    codes are no longer minted here either — an abandoned setup used to wipe
    the working codes of an already-enrolled account.
    """
    active = _mfa_method(db, current_user.id, verified_only=True)
    if active:
        raise HTTPException(
            status_code=409,
            detail="Two-factor authentication is already on. Turn it off before setting up a new authenticator.",
        )

    secret = generate_totp_secret()
    method = _mfa_method(db, current_user.id)
    if method:
        method.secret_encrypted = secret
        method.verified_at = None
    else:
        method = UserMFAMethod(user_id=current_user.id, method_type="totp", secret_encrypted=secret)
        db.add(method)

    log_security_audit_event(
        db,
        action="auth.mfa.enroll_started",
        resource_type="user",
        resource_id=str(current_user.id),
        actor_user_id=current_user.id,
        actor_type="user",
    )
    db.commit()
    return {
        "secret": secret,
        "otpauth_url": build_otpauth_url(secret, current_user.email),
        "account": current_user.email,
        "issuer": TOTP_ISSUER,
    }


@router.post("/mfa/verify")
def verify_mfa_setup(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Confirm the authenticator is in sync, activate it, and issue recovery codes."""
    code = (body.get("code") or "").strip()
    method = _mfa_method(db, current_user.id)
    if not method:
        raise HTTPException(status_code=404, detail="Start setup before entering a code")
    if method.verified_at:
        raise HTTPException(status_code=409, detail="Two-factor authentication is already on")

    _guard_mfa_attempts(db, current_user.id, "enroll")
    if not verify_totp_code(method.secret_encrypted, code):
        remaining = record_failed_mfa_attempt(f"enroll:{current_user.id}")
        log_security_audit_event(
            db,
            action="auth.mfa.enroll_code_rejected",
            resource_type="user",
            resource_id=str(current_user.id),
            actor_user_id=current_user.id,
            actor_type="user",
            outcome="failure",
            metadata={"attempts_remaining": remaining},
        )
        db.commit()
        raise HTTPException(
            status_code=400,
            detail="That code didn't match. Check your authenticator and try the current code.",
        )

    clear_mfa_attempts(f"enroll:{current_user.id}")
    method.verified_at = datetime.utcnow()
    method.label = method.label or "Authenticator app"
    current_user.mfa_required = True
    settings = get_or_create_user_settings(db, current_user.id)
    settings.two_factor = True
    backup_codes = _issue_recovery_codes(db, current_user.id)

    log_security_audit_event(
        db,
        action="auth.mfa.enabled",
        resource_type="user",
        resource_id=str(current_user.id),
        actor_user_id=current_user.id,
        actor_type="user",
    )
    db.commit()
    return {"ok": True, "backup_codes": backup_codes}


@router.post("/mfa/disable")
def disable_mfa(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Turn off two-factor. Requires a current code — a hijacked session must
    not be able to strip the second factor off the account."""
    method = _mfa_method(db, current_user.id, verified_only=True)
    if not method:
        raise HTTPException(status_code=404, detail="Two-factor authentication is not on")
    if _workspace_requires_mfa(db, current_user.id):
        raise HTTPException(
            status_code=409,
            detail="A Drive you belong to requires two-factor authentication. Ask an owner to lift the policy first.",
        )

    _guard_mfa_attempts(db, current_user.id, "disable")
    code = (body.get("code") or "").strip()
    recovery_code = (body.get("recovery_code") or "").strip()
    if not _verify_second_factor(db, current_user, code=code, recovery_code=recovery_code):
        remaining = record_failed_mfa_attempt(f"disable:{current_user.id}")
        log_security_audit_event(
            db,
            action="auth.mfa.disable_rejected",
            resource_type="user",
            resource_id=str(current_user.id),
            actor_user_id=current_user.id,
            actor_type="user",
            outcome="failure",
            metadata={"attempts_remaining": remaining},
        )
        db.commit()
        raise HTTPException(status_code=400, detail="That code didn't match.")

    clear_mfa_attempts(f"disable:{current_user.id}")
    _disable_mfa_rows(db, current_user)
    log_security_audit_event(
        db,
        action="auth.mfa.disabled",
        resource_type="user",
        resource_id=str(current_user.id),
        actor_user_id=current_user.id,
        actor_type="user",
    )
    db.commit()
    return {"ok": True}


@router.post("/mfa/recovery-codes")
def regenerate_recovery_codes(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Issue a fresh set, invalidating the old one. Requires a current TOTP
    code — recovery codes are a login credential, not a display value."""
    method = _mfa_method(db, current_user.id, verified_only=True)
    if not method:
        raise HTTPException(status_code=404, detail="Two-factor authentication is not on")

    _guard_mfa_attempts(db, current_user.id, "recovery")
    code = (body.get("code") or "").strip()
    if not verify_totp_code(method.secret_encrypted, code):
        remaining = record_failed_mfa_attempt(f"recovery:{current_user.id}")
        log_security_audit_event(
            db,
            action="auth.mfa.recovery_regenerate_rejected",
            resource_type="user",
            resource_id=str(current_user.id),
            actor_user_id=current_user.id,
            actor_type="user",
            outcome="failure",
            metadata={"attempts_remaining": remaining},
        )
        db.commit()
        raise HTTPException(status_code=400, detail="That code didn't match.")

    clear_mfa_attempts(f"recovery:{current_user.id}")
    backup_codes = _issue_recovery_codes(db, current_user.id)
    log_security_audit_event(
        db,
        action="auth.mfa.recovery_codes_regenerated",
        resource_type="user",
        resource_id=str(current_user.id),
        actor_user_id=current_user.id,
        actor_type="user",
    )
    db.commit()
    return {"backup_codes": backup_codes}


@router.post("/mfa/challenge")
def complete_mfa_challenge(body: dict, db: Session = Depends(get_db)):
    """Second leg of sign-in: exchange the challenge token plus a code for a session."""
    challenge_token = body.get("challenge_token")
    if not challenge_token:
        raise HTTPException(status_code=400, detail="Missing challenge token")
    payload = decode_access_token_payload(challenge_token)
    if not payload.get("mfa_pending"):
        raise HTTPException(status_code=400, detail="Invalid MFA challenge token")
    user_id = int(payload.get("user_id"))
    user = db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not _mfa_method(db, user.id, verified_only=True):
        raise HTTPException(status_code=400, detail="No verified MFA method")

    _guard_mfa_attempts(db, user.id, "challenge")
    code = (body.get("code") or "").strip()
    recovery_code = (body.get("recovery_code") or "").strip()
    if not _verify_second_factor(db, user, code=code, recovery_code=recovery_code):
        remaining = record_failed_mfa_attempt(f"challenge:{user.id}")
        log_security_audit_event(
            db,
            action="auth.mfa.challenge_failed",
            resource_type="user",
            resource_id=str(user.id),
            actor_user_id=user.id,
            actor_type="user",
            outcome="failure",
            metadata={"attempts_remaining": remaining},
        )
        db.commit()
        raise HTTPException(status_code=401, detail="Invalid MFA code")

    clear_mfa_attempts(f"challenge:{user.id}")
    session_id = create_user_session(db, user.id)
    access_token = create_access_token(
        data={"user_id": user.id, "onboarding_completed": user.onboarding_completed, "sid": session_id}
    )
    refresh_token = create_refresh_token(data={"user_id": user.id, "sid": session_id})
    log_security_audit_event(
        db,
        action="auth.mfa.challenge_passed",
        resource_type="user",
        resource_id=str(user.id),
        actor_user_id=user.id,
        actor_type="user",
    )
    db.commit()
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "refresh_token": refresh_token,
        "onboarding_completed": user.onboarding_completed,
        # Surfaced as a warning banner once the list runs low.
        "recovery_codes_remaining": _unused_recovery_code_count(db, user.id),
    }


class RefreshTokenBody(BaseModel):
    refresh_token: str


# Implement a token refresh endpoint
@router.post("/refresh-token")
def refresh_access_token(body: RefreshTokenBody, db: Session = Depends(get_db)):
    payload = verify_refresh_token(body.refresh_token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    user_id = validate_refresh_session(db, payload)
    user = db.query(User).filter(User.id == int(user_id)).first()
    if not user:  # defensive; validate_refresh_session already checks this
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    session_id = payload.get("sid")

    access_token = create_access_token(
        data={"user_id": user.id, "onboarding_completed": user.onboarding_completed, "sid": session_id}
    )
    return {"access_token": access_token, "token_type": "bearer"}


@router.post("/sync-access-token")
def sync_access_token(
    token: str = Depends(oauth2_scheme),
    current_user: User = Depends(get_current_user),
):
    """Mint a new access token with current onboarding flags (e.g. after Stripe checkout)."""
    payload = decode_access_token_payload(token)
    session_id = payload.get("sid")
    if not session_id:
        raise HTTPException(status_code=401, detail="Session missing. Please sign in again.")
    access_token = create_access_token(
        data={
            "user_id": current_user.id,
            "onboarding_completed": current_user.onboarding_completed,
            "sid": session_id,
        }
    )
    return {"access_token": access_token, "token_type": "bearer"}


@router.post("/logout")
def logout_user(
    token: str = Depends(oauth2_scheme),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    payload = decode_access_token_payload(token)
    session_id = payload.get("sid")
    if not session_id:
        raise HTTPException(status_code=401, detail="Session missing. Please sign in again.")
    revoke_user_session(db, current_user.id, session_id)
    return {"ok": True}


# ── Onboarding Endpoints (must be before /{user_id} to avoid route conflict) ──

@router.get("/me", response_model=UserResponse)
def get_current_user_profile(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return current_user


@router.get("/me/settings", response_model=UserSettingsResponse)
def get_current_user_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return get_or_create_user_settings(db, current_user.id)


def _owned_workspace(db: Session, user_id: int) -> Workspace | None:
    """The personal workspace a user owns, if any. Used to keep the Drive name
    in `user_settings` and the `Workspace` row from drifting apart."""
    return (
        db.query(Workspace)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .filter(
            WorkspaceMember.user_id == user_id,
            WorkspaceMember.role == "owner",
            Workspace.owner_user_id == user_id,
        )
        .order_by(Workspace.id.asc())
        .first()
    )


@router.put("/me/settings", response_model=UserSettingsResponse)
def update_current_user_settings(
    data: UserSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    settings = get_or_create_user_settings(db, current_user.id)
    update_data = data.model_dump(exclude_unset=True)

    if "theme" in update_data and update_data["theme"] not in {"light", "dark", "system"}:
        raise HTTPException(status_code=400, detail="Invalid theme")

    if "session_timeout" in update_data and update_data["session_timeout"] not in {"15", "30", "60", "120"}:
        raise HTTPException(status_code=400, detail="Invalid session timeout")
    if "timezone" in update_data and update_data["timezone"] not in ALLOWED_TIMEZONES:
        raise HTTPException(status_code=400, detail="Invalid timezone")
    if "date_format" in update_data and update_data["date_format"] not in ALLOWED_DATE_FORMATS:
        raise HTTPException(status_code=400, detail="Invalid date format")
    if "workspace_name" in update_data:
        value = (update_data["workspace_name"] or "").strip()
        if not value:
            raise HTTPException(status_code=400, detail="Workspace name cannot be empty")
        update_data["workspace_name"] = value[:120]
    if "email_mention_digest" in update_data and update_data["email_mention_digest"] not in {
        "off",
        "daily",
        "weekly",
    }:
        raise HTTPException(status_code=400, detail="Invalid mention digest setting")

    # `two_factor` mirrors whether an authenticator is enrolled; it is not an
    # independent preference. Writing it here used to be enough to require a
    # second factor the account had no way to produce, locking the user out at
    # the next sign-in. Enrollment owns this flag — see /mfa/verify and
    # /mfa/disable.
    update_data.pop("two_factor", None)

    for key, value in update_data.items():
        setattr(settings, key, value)

    # Settings presents this as "Drive name", but it only ever wrote
    # `user_settings.workspace_name` — so renaming a Drive left the Workspace
    # row, and everything reading it (member lists, invite emails, share
    # links), on the old name. Rename the workspace the user *owns*; a
    # workspace they merely belong to isn't theirs to relabel.
    if "workspace_name" in update_data:
        owned = _owned_workspace(db, current_user.id)
        if owned:
            owned.name = update_data["workspace_name"]

    db.commit()
    db.refresh(settings)
    return settings


# --- Caption template favorites ----------------------------------------------
#
# Caption templates are catalogued client-side (see editube-frontend CAPTION_TEMPLATES).
# We persist the user's favorited template ids only — never the template config — so
# adding/changing templates does not require migrations. `template_id` is rate-limited
# to 80 characters and stripped of whitespace.
_CAPTION_FAVORITE_ID_MAX = 80


def _normalize_caption_template_id(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="template_id is required")
    if len(value) > _CAPTION_FAVORITE_ID_MAX:
        raise HTTPException(status_code=400, detail="template_id is too long")
    return value


class CaptionFavoriteCreate(BaseModel):
    template_id: str


class CaptionFavoriteResponse(BaseModel):
    template_id: str
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get("/me/caption-favorites", response_model=list[CaptionFavoriteResponse])
def list_caption_favorites(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (
        db.query(UserCaptionFavorite)
        .filter(UserCaptionFavorite.user_id == current_user.id)
        .order_by(UserCaptionFavorite.created_at.desc())
        .all()
    )
    return rows


@router.post(
    "/me/caption-favorites",
    response_model=CaptionFavoriteResponse,
    status_code=201,
)
def add_caption_favorite(
    data: CaptionFavoriteCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    template_id = _normalize_caption_template_id(data.template_id)
    existing = (
        db.query(UserCaptionFavorite)
        .filter(
            UserCaptionFavorite.user_id == current_user.id,
            UserCaptionFavorite.template_id == template_id,
        )
        .first()
    )
    if existing:
        return existing
    row = UserCaptionFavorite(user_id=current_user.id, template_id=template_id)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/me/caption-favorites/{template_id}", status_code=204)
def remove_caption_favorite(
    template_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    normalized = _normalize_caption_template_id(template_id)
    deleted = (
        db.query(UserCaptionFavorite)
        .filter(
            UserCaptionFavorite.user_id == current_user.id,
            UserCaptionFavorite.template_id == normalized,
        )
        .delete(synchronize_session=False)
    )
    if not deleted:
        # Idempotent: treat missing favorites as already-gone rather than 404'ing
        # the optimistic toggle.
        return
    db.commit()


@router.put("/onboarding/profile", response_model=UserResponse)
def onboarding_update_profile(
    data: OnboardingProfileUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    current_user.full_name = data.full_name
    if data.phone is not None:
        current_user.phone = data.phone
    if data.avatar_url is not None:
        current_user.avatar_url = data.avatar_url
    db.commit()
    db.refresh(current_user)
    return current_user


@router.post("/onboarding/avatar")
def onboarding_upload_avatar(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    file_url = upload_file_to_cloudinary(file, resource_type="image")
    current_user.avatar_url = file_url
    db.commit()
    db.refresh(current_user)
    return {"avatar_url": file_url}


@router.put("/onboarding/workflow", response_model=UserResponse)
def onboarding_update_workflow(
    data: OnboardingWorkflowUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Onboarding asks which of the product's workflows the user came for, not
    # what kind of company they are. The old org-type values ("agency",
    # "freelancer", "internal") are still readable on existing rows but are no
    # longer accepted on write.
    allowed = ("auto_edit", "repurpose", "review")

    # Dedupe while keeping the order they were picked in — that order is the
    # signal about what the user cares about most.
    selected: list[str] = []
    for value in data.workflow_types:
        if value not in allowed:
            raise HTTPException(status_code=400, detail="Invalid workflow type")
        if value not in selected:
            selected.append(value)

    if not selected:
        raise HTTPException(status_code=400, detail="Select at least one workflow")

    current_user.workflow_types = selected
    db.commit()
    db.refresh(current_user)
    return current_user


@router.put("/onboarding/plan", response_model=UserResponse)
def onboarding_update_plan(
    data: OnboardingPlanUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Persist selected plan before Checkout. Onboarding completes after Stripe webhook."""
    normalized = normalize_plan_key(data.plan)
    if normalized not in ("free", "pro", "scale", "enterprise"):
        raise HTTPException(status_code=400, detail="Invalid plan")
    current_user.plan = normalized
    db.commit()
    db.refresh(current_user)
    return current_user


@router.post("/onboarding/complete-free", response_model=UserResponse)
def onboarding_complete_free(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    current_user.plan = "free"
    current_user.onboarding_completed = True
    if current_user.trial_start_date is None:
        current_user.trial_start_date = datetime.utcnow()
    db.commit()
    db.refresh(current_user)
    return current_user


@router.get("/sso/login")
def sso_login_redirect(
    email: str = Query(..., min_length=3),
    return_path: str = Query(default="/"),
    db: Session = Depends(get_db),
):
    domain = (email.split("@")[-1] if "@" in email else "").strip().lower()
    if not domain:
        raise HTTPException(status_code=400, detail="Invalid email")
    provider = (
        db.query(WorkspaceSSOProvider)
        .filter(
            WorkspaceSSOProvider.domain_hint == domain,
            WorkspaceSSOProvider.enabled.is_(True),
        )
        .first()
    )
    if not provider:
        raise HTTPException(status_code=404, detail="No SSO provider configured for this domain")
    policy = (
        db.query(WorkspaceAuthPolicy)
        .filter(WorkspaceAuthPolicy.workspace_id == provider.workspace_id)
        .first()
    )
    if policy and not policy.enforce_sso and provider.provider == "google":
        raise HTTPException(status_code=400, detail="Workspace does not require SSO for this domain")
    # Keep callback deterministic from env so providers can whitelist one URL.
    callback = f"{os.getenv('BACKEND_BASE_URL', 'http://localhost:8000').rstrip('/')}/api/users/sso/callback"
    _redirect_uri, _nonce_state = build_oidc_authorize_url(provider, redirect_uri=callback)
    state = build_signed_sso_state(provider_id=provider.id, return_path=return_path)
    endpoint = provider.authorization_endpoint or f"{provider.issuer.rstrip('/')}/v1/authorize"
    from urllib import parse
    params = parse.urlencode(
        {
            "client_id": provider.client_id,
            "redirect_uri": callback,
            "response_type": "code",
            "scope": provider.scope or "openid profile email",
            "state": state,
        }
    )
    redirect_uri = f"{endpoint}?{params}"
    log_security_audit_event(
        db,
        action="auth.sso.login_redirect",
        resource_type="workspace_sso_provider",
        resource_id=str(provider.id),
        actor_type="anonymous",
        workspace_id=provider.workspace_id,
        metadata={"email_domain": domain, "return_path": return_path, "state": state},
    )
    db.commit()
    return RedirectResponse(url=redirect_uri, status_code=302)


@router.get("/sso/callback")
def sso_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    frontend_base = os.getenv("FRONTEND_BASE_URL", "http://localhost:3000").rstrip("/")
    if error or not code:
        reason = error or "missing_code"
        return RedirectResponse(url=f"{frontend_base}/login?sso_error={reason}", status_code=302)
    if not state:
        return RedirectResponse(url=f"{frontend_base}/login?sso_error=missing_state", status_code=302)

    try:
        state_payload = verify_signed_sso_state(state)
    except ValueError:
        return RedirectResponse(url=f"{frontend_base}/login?sso_error=invalid_state", status_code=302)
    provider_id = int(state_payload.get("provider_id"))
    provider = (
        db.query(WorkspaceSSOProvider)
        .filter(
            WorkspaceSSOProvider.id == provider_id,
            WorkspaceSSOProvider.enabled.is_(True),
        )
        .first()
    )
    if not provider:
        return RedirectResponse(url=f"{frontend_base}/login?sso_error=provider_not_found", status_code=302)
    return_path = str(state_payload.get("return_path") or "/")
    token_endpoint = provider.token_endpoint or f"{provider.issuer.rstrip('/')}/v1/token"
    userinfo_endpoint = provider.userinfo_endpoint or f"{provider.issuer.rstrip('/')}/v1/userinfo"
    callback = f"{os.getenv('BACKEND_BASE_URL', 'http://localhost:8000').rstrip('/')}/api/users/sso/callback"
    try:
        token_data = exchange_oidc_code(
            token_endpoint=token_endpoint,
            code=code,
            client_id=provider.client_id,
            client_secret=provider.client_secret_encrypted,
            redirect_uri=callback,
        )
        access_token_upstream = token_data.get("access_token")
        if not access_token_upstream:
            raise HTTPException(status_code=400, detail="missing_access_token")
        profile = fetch_oidc_userinfo(userinfo_endpoint=userinfo_endpoint, access_token=access_token_upstream)
        claims = validate_oidc_claims(
            provider=provider,
            id_token=token_data.get("id_token"),
            userinfo=profile,
        )
    except Exception:
        return RedirectResponse(url=f"{frontend_base}/login?sso_error=exchange_failed", status_code=302)
    email = (claims.get("email") or profile.get("email") or "").strip().lower()
    name = (claims.get("name") or profile.get("name") or email.split("@")[0] or "SSO User").strip()
    if not email:
        return RedirectResponse(url=f"{frontend_base}/login?sso_error=missing_email", status_code=302)
    user = db.query(User).filter(User.email == email).first()
    if not user:
        user = User(
            email=email,
            name=name,
            full_name=name,
            role="user",
            hashed_password=None,
            auth_provider="sso",
        )
        db.add(user)
        db.flush()
        db.add(WorkspaceMember(workspace_id=provider.workspace_id, user_id=user.id, role="editor"))
    elif (user.auth_provider or "local") == "local":
        user.auth_provider = "sso"

    # An enrolled authenticator applies here too — the identity provider proves
    # who they are, not that they hold the second factor this account requires.
    if _mfa_method(db, user.id, verified_only=True):
        challenge_token = create_access_token(
            data={
                "user_id": user.id,
                "onboarding_completed": user.onboarding_completed,
                "mfa_pending": True,
            },
            expires_minutes=MFA_CHALLENGE_TTL_MINUTES,
        )
        log_security_audit_event(
            db,
            action="auth.sso.callback_mfa_required",
            resource_type="workspace_sso_provider",
            resource_id=str(provider.id),
            actor_user_id=user.id,
            actor_type="user",
            workspace_id=provider.workspace_id,
            metadata={"email": email},
        )
        db.commit()
        return RedirectResponse(
            url=(
                f"{frontend_base}/google/callback?mfa_challenge={url_quote(challenge_token, safe='')}"
                f"&next={url_quote(return_path, safe='')}"
            ),
            status_code=302,
        )

    session_id = create_user_session(db, user.id)
    app_access_token = create_access_token(
        data={"user_id": user.id, "onboarding_completed": user.onboarding_completed, "sid": session_id}
    )
    app_refresh_token = create_refresh_token(data={"user_id": user.id, "sid": session_id})
    log_security_audit_event(
        db,
        action="auth.sso.callback_success",
        resource_type="workspace_sso_provider",
        resource_id=str(provider.id),
        actor_user_id=user.id,
        actor_type="user",
        workspace_id=provider.workspace_id,
        metadata={"email": email, "state": state},
    )
    db.commit()
    return RedirectResponse(
        url=(
            f"{frontend_base}/google/callback?access_token={app_access_token}"
            f"&refresh_token={app_refresh_token}"
            f"&onboarding_completed={'true' if user.onboarding_completed else 'false'}"
            f"&next={url_quote(return_path, safe='')}"
        ),
        status_code=302,
    )


class GoogleMobileTokenRequest(BaseModel):
    id_token: str


@router.post("/sso/google/mobile")
def sso_google_mobile(payload: GoogleMobileTokenRequest, db: Session = Depends(get_db)):
    import json as _json
    from urllib import parse as _parse, request as _request
    from urllib.error import URLError as _URLError

    token = (payload.id_token or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Missing id_token")

    verify_url = f"https://oauth2.googleapis.com/tokeninfo?id_token={_parse.quote(token)}"
    try:
        with _request.urlopen(verify_url, timeout=15) as resp:
            info = _json.loads(resp.read().decode("utf-8"))
    except _URLError:
        raise HTTPException(status_code=400, detail="Unable to verify Google token")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid Google id_token")

    if info.get("error") or info.get("error_description"):
        raise HTTPException(status_code=400, detail="Invalid Google id_token")

    expected_client_ids = {
        cid.strip()
        for cid in os.getenv("GOOGLE_MOBILE_CLIENT_IDS", "").split(",")
        if cid.strip()
    }
    aud = (info.get("aud") or "").strip()
    if expected_client_ids and aud not in expected_client_ids:
        raise HTTPException(status_code=400, detail="Google audience mismatch")

    iss = (info.get("iss") or "").rstrip("/")
    if iss not in {"accounts.google.com", "https://accounts.google.com"}:
        raise HTTPException(status_code=400, detail="Invalid Google issuer")

    email_verified = info.get("email_verified")
    if email_verified not in (True, "true"):
        raise HTTPException(status_code=400, detail="Google email not verified")

    email = (info.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Google profile missing email")
    name = (info.get("name") or email.split("@")[0] or "Google User").strip()

    user = db.query(User).filter(User.email == email).first()
    created_new = False
    if not user:
        user = User(
            email=email,
            name=name,
            full_name=name,
            role="user",
            hashed_password=None,
            auth_provider="sso",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        from app.services.workspace_bootstrap import ensure_personal_workspace

        ensure_personal_workspace(db, user)
        created_new = True
    elif (user.auth_provider or "local") == "local":
        user.auth_provider = "sso"

    # Same shape the password login returns, so the mobile client can reuse its
    # existing challenge screen rather than skipping the second factor.
    if _mfa_method(db, user.id, verified_only=True):
        challenge_token = create_access_token(
            data={
                "user_id": user.id,
                "onboarding_completed": user.onboarding_completed,
                "mfa_pending": True,
            },
            expires_minutes=MFA_CHALLENGE_TTL_MINUTES,
        )
        log_security_audit_event(
            db,
            action="auth.sso.google_mobile_mfa_required",
            resource_type="user",
            resource_id=str(user.id),
            actor_user_id=user.id,
            actor_type="user",
            metadata={"email": email},
        )
        db.commit()
        return {"mfa_required": True, "challenge_token": challenge_token}

    session_id = create_user_session(db, user.id)
    access_token = create_access_token(
        data={
            "user_id": user.id,
            "onboarding_completed": user.onboarding_completed,
            "sid": session_id,
        }
    )
    refresh_token = create_refresh_token(data={"user_id": user.id, "sid": session_id})
    log_security_audit_event(
        db,
        action="auth.sso.google_mobile_success",
        resource_type="user",
        resource_id=str(user.id),
        actor_user_id=user.id,
        actor_type="user",
        metadata={"email": email, "created": created_new},
    )
    db.commit()
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "onboarding_completed": bool(user.onboarding_completed),
    }


# ── User CRUD (parameterized routes last) ──

@router.get("/{user_id}", response_model=UserResponse)
def get_user(user_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to access this user")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.put("/{user_id}")
def update_user(user_id: int, user: UserUpdate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if current_user.id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized to update this user")
    db_user = db.query(User).filter(User.id == user_id).first()
    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")
    update_data = user.dict(exclude_unset=True)
    if update_data.get("password"):
        hashed_password = get_password_hash(update_data["password"])
        update_data["hashed_password"] = hashed_password
        del update_data["password"]
    for key, value in update_data.items():
        setattr(db_user, key, value)
    db.commit()
    db.refresh(db_user)
    return db_user