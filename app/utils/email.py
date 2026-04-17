"""Transactional email via SMTP (env-configured)."""

from __future__ import annotations

import logging
import os
import smtplib
from datetime import date, datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import stripe
from sqlalchemy.orm import Session

from app.db.models import Project, User

logger = logging.getLogger(__name__)


def _fmt_long_date_utc(dt: datetime) -> str:
    """e.g. April 17, 2026 — avoids platform-specific %-d in strftime."""
    return f"{dt.astimezone(timezone.utc).strftime('%B')} {dt.astimezone(timezone.utc).day}, {dt.astimezone(timezone.utc).year}"


def _smtp_settings() -> tuple[str | None, int, str | None, str | None, str | None, bool]:
    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    from_addr = os.getenv("EMAIL_FROM") or user
    use_tls = os.getenv("SMTP_USE_TLS", "true").lower() in ("1", "true", "yes")
    return host, port, user, password, from_addr, use_tls


def is_smtp_configured() -> bool:
    host, _, user, password, from_addr, _ = _smtp_settings()
    return bool(host and user and password and from_addr)


def send_transactional_email(
    to: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
) -> bool:
    """
    Send a single email. Returns False if SMTP is not configured or send fails.
    Failures are logged; callers should not raise for webhook safety.
    """
    if not is_smtp_configured():
        logger.warning("SMTP not configured; skipping email to %s", to)
        return False

    host, port, user, password, from_addr, use_tls = _smtp_settings()
    assert host and user and password and from_addr

    if body_html:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body_text, "plain", "utf-8"))
        msg.attach(MIMEText(body_html, "html", "utf-8"))
    else:
        msg = MIMEText(body_text, "plain", "utf-8")

    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to

    try:
        with smtplib.SMTP(host, port, timeout=30) as server:
            if use_tls:
                server.starttls()
            server.login(user, password)
            server.sendmail(from_addr, [to], msg.as_string())
        return True
    except Exception:
        logger.exception("Failed to send email to %s", to)
        return False


def get_invitor_name(db: Session, creator_id: int) -> str:
    creator = db.query(User).filter(User.id == creator_id).first()
    return creator.name if creator else "Unknown User"


def send_invitation_email(db: Session, email: str, project_id: int) -> bool:
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return False
    invitor_name = get_invitor_name(db, project.creator_id)
    base = os.getenv("FRONTEND_BASE_URL", "http://localhost:3000").rstrip("/")
    body = (
        f"You have been invited by {invitor_name} to collaborate on Editube.\n\n"
        f"Project: {project.name}\n"
        f"Create an account or sign in: {base}/signup\n"
    )
    return send_transactional_email(
        email,
        f"Invitation to collaborate: {project.name}",
        body,
    )


def _plan_label(plan: str | None) -> str:
    if not plan:
        return "your plan"
    return {
        "free": "Free",
        "pro": "Pro",
        "scale": "Scale",
        "enterprise": "Enterprise",
        "basic": "Free",
        "elite": "Scale",
    }.get(plan.lower(), plan)


def _sub_get(sub: stripe.Subscription, key: str):
    """StripeObject raises on missing keys; subscription payloads omit some fields while trialing."""
    try:
        return sub[key]
    except (KeyError, TypeError, AttributeError):
        return None


def send_subscription_welcome_email(user: User, sub: stripe.Subscription) -> None:
    """Called after successful checkout webhook sync. Logs-only on failure."""
    if not user.email:
        return
    plan = _plan_label(user.plan or _subscription_plan_from_stripe(sub))
    name = user.full_name or user.name or "there"
    base = os.getenv("FRONTEND_BASE_URL", "http://localhost:3000").rstrip("/")
    manage_url = f"{base}/account?tab=billing"

    status_val = _sub_get(sub, "status") or getattr(sub, "status", None)
    trial_end = _sub_get(sub, "trial_end")
    trial_start = _sub_get(sub, "trial_start")
    current_period_end = _sub_get(sub, "current_period_end")
    sub_id = _sub_get(sub, "id") or getattr(sub, "id", None)
    created_ts = _sub_get(sub, "created")

    order_date = datetime.now(timezone.utc)
    if isinstance(created_ts, (int, float)):
        order_date = datetime.fromtimestamp(created_ts, tz=timezone.utc)

    trial_lines = ""
    if status_val == "trialing" and trial_end:
        end = datetime.fromtimestamp(int(trial_end), tz=timezone.utc)
        trial_lines = (
            f"You have successfully subscribed to Editube {plan}.\n"
            f"Enjoy your trial. Your trial is active until {_fmt_long_date_utc(end)} (UTC).\n"
            f"After your trial, your subscription renews according to the billing interval you chose.\n"
        )
    elif trial_start and trial_end:
        ts = datetime.fromtimestamp(int(trial_start), tz=timezone.utc)
        te = datetime.fromtimestamp(int(trial_end), tz=timezone.utc)
        trial_lines = (
            f"Trial period: {_fmt_long_date_utc(ts)} – {_fmt_long_date_utc(te)} (UTC).\n"
        )
    else:
        trial_lines = f"You have successfully subscribed to Editube {plan}.\n"

    renewal_line = ""
    if current_period_end and status_val == "active":
        end = datetime.fromtimestamp(int(current_period_end), tz=timezone.utc)
        renewal_line = f"Your current billing period ends {_fmt_long_date_utc(end)} (UTC).\n"

    text = (
        f"Hi {name},\n\n"
        f"{trial_lines}"
        f"{renewal_line}\n"
        f"Manage your subscription: {manage_url}\n\n"
        f"If you have any questions, reply to this email or visit our help center.\n\n"
        f"— The Editube team\n\n"
        f"Order number: {sub_id or '—'}\n"
        f"Order date: {order_date.astimezone(timezone.utc).strftime('%b')} {order_date.astimezone(timezone.utc).day}, {order_date.astimezone(timezone.utc).year}\n"
        f"Plan: Editube {plan} subscription\n"
    )
    send_transactional_email(
        user.email,
        f"You are subscribed to Editube {plan}",
        text,
    )


def _subscription_plan_from_stripe(sub: stripe.Subscription) -> str | None:
    meta = _sub_get(sub, "metadata")
    if not meta:
        return None
    try:
        p = meta.get("plan") if hasattr(meta, "get") else meta["plan"]
        if p in ("free", "pro", "scale", "enterprise"):
            return p
        if p == "basic":
            return "free"
        if p == "elite":
            return "scale"
        return None
    except (AttributeError, TypeError, KeyError):
        return None


def send_subscription_canceled_email(
    to_email: str,
    display_name: str,
    plan: str | None,
    *,
    access_until: datetime | None = None,
    stripe_subscription_id: str | None = None,
) -> None:
    """Called after subscription deleted webhook. Logs-only on failure."""
    if not to_email:
        return
    pl = _plan_label(plan)
    base = os.getenv("FRONTEND_BASE_URL", "http://localhost:3000").rstrip("/")
    billing_url = f"{base}/account?tab=billing"
    until = ""
    if access_until:
        if access_until.tzinfo is None:
            access_until = access_until.replace(tzinfo=timezone.utc)
        until = (
            f"Your {pl} subscription will not renew and has ended in Stripe, "
            f"but paid features remain available until the end of your billing period on "
            f"{_fmt_long_date_utc(access_until.astimezone(timezone.utc))} (UTC).\n\n"
        )
    else:
        until = f"Your Editube {pl} subscription has been canceled. You will not be charged again.\n\n"

    text = (
        f"Hi {display_name},\n\n"
        f"{until}"
        f"If you change your mind, you can pick a plan again here: {billing_url}\n\n"
        f"If you have any questions, please contact us through our help center.\n\n"
        f"Best,\n"
        f"The Editube team\n\n"
        f"You received this email because you have a paid Editube account.\n"
    )
    if stripe_subscription_id:
        text += f"Subscription reference: {stripe_subscription_id}\n"
    send_transactional_email(
        to_email,
        "Your Editube subscription has ended",
        text,
    )


def send_subscription_will_not_renew_email(
    to_email: str,
    display_name: str,
    plan: str | None,
    period_end: datetime | date | None,
) -> None:
    """User turned off auto-renew; subscription stays active until period end."""
    if not to_email:
        return
    pl = _plan_label(plan)
    base = os.getenv("FRONTEND_BASE_URL", "http://localhost:3000").rstrip("/")
    billing_url = f"{base}/account?tab=billing"
    end_txt = "the end of your current billing period"
    if period_end:
        dt = period_end
        if isinstance(dt, datetime) and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if isinstance(dt, datetime):
            end_txt = _fmt_long_date_utc(dt)
        else:
            end_txt = f"{dt.strftime('%B')} {dt.day}, {dt.year}"

    text = (
        f"Hi {display_name},\n\n"
        f"Your Editube {pl} subscription will not renew and is scheduled to cancel, "
        f"but remains fully available until {end_txt} (UTC).\n\n"
        f"If you change your mind, you can manage or renew your subscription here: {billing_url}\n\n"
        f"If you have any questions, please contact us through our help center.\n\n"
        f"Best,\n"
        f"The Editube team\n"
    )
    send_transactional_email(
        to_email,
        "Your Editube subscription will not renew",
        text,
    )


def send_review_magic_link_email(
    to_email: str,
    review_label: str,
    verify_url: str,
    expires_minutes: int = 20,
    recipient_name: str | None = None,
    inviter_name: str | None = None,
) -> bool:
    subject = f"Secure review link: {review_label}"
    greet = f"Hi {recipient_name}," if recipient_name else "Hi,"
    inviter_line = (
        f"{inviter_name} invited you to review this video.\n\n"
        if inviter_name
        else "You requested access to this review.\n\n"
    )
    text = (
        f"{greet}\n\n"
        f"{inviter_line}"
        f"Review: {review_label}\n\n"
        f"Open your one-time secure link:\n{verify_url}\n\n"
        "This email link is tied to this recipient address.\n"
        f"This link expires in {expires_minutes} minutes and can only be used once.\n"
        "If you did not request this, you can ignore this email."
    )
    html = f"""
    <p>{greet}</p>
    <p>{inviter_name + " invited you to review this video." if inviter_name else "You requested access to this review."}</p>
    <p>Review: <strong>{review_label}</strong></p>
    <p><a href="{verify_url}">Open one-time secure review link</a></p>
    <p>This email link is tied to this recipient address.</p>
    <p>This link expires in {expires_minutes} minutes and can only be used once.</p>
    <p>If you did not request this, you can ignore this email.</p>
    """
    return send_transactional_email(to_email, subject, text, html)


def send_workspace_invite_email(
    to_email: str,
    workspace_name: str,
    inviter_name: str,
    invite_role: str,
    invite_url: str,
    expires_days: int = 14,
) -> bool:
    """Notify invitee to sign in with this email and open the workspace invite link."""
    subject = f"You are invited to {workspace_name} on Editube"
    role_line = f"You will join as: {invite_role}.\n\n" if invite_role else ""
    text = (
        f"Hi,\n\n"
        f"{inviter_name} invited you to join the workspace \"{workspace_name}\" on Editube.\n\n"
        f"{role_line}"
        f"Important: sign in with this same email address ({to_email}) to accept the invite.\n\n"
        f"Accept your invite (link expires in {expires_days} days):\n{invite_url}\n\n"
        "If you do not have an account yet, create one with this email, then open the link again.\n\n"
        "If you were not expecting this, you can ignore this email."
    )
    role_html = (
        f"<p>You will join as: <strong>{invite_role}</strong>.</p>" if invite_role else ""
    )
    html = f"""
    <p>Hi,</p>
    <p><strong>{inviter_name}</strong> invited you to join the workspace
    <strong>{workspace_name}</strong> on Editube.</p>
    {role_html}
    <p>Sign in with this same email address (<strong>{to_email}</strong>) to accept the invite.</p>
    <p><a href="{invite_url}">Accept workspace invite</a></p>
    <p>This link expires in {expires_days} days.</p>
    <p>If you do not have an account yet, create one with this email, then open the link again.</p>
    <p>If you were not expecting this, you can ignore this email.</p>
    """
    return send_transactional_email(to_email, subject, text, html)


def send_workspace_provisioned_account_email(
    to_email: str,
    display_name: str,
    workspace_name: str,
    inviter_name: str,
    login_url: str,
    temporary_password: str,
    workspace_role: str,
) -> bool:
    """Send login email after workspace owner created a password account for this address."""
    subject = f"Your Editube login for {workspace_name}"
    greet = f"Hi {display_name}," if display_name else "Hi,"
    text = (
        f"{greet}\n\n"
        f"{inviter_name} created an Editube account for you to join the workspace \"{workspace_name}\".\n"
        f"You will have the role: {workspace_role}.\n\n"
        f"Sign in here: {login_url}\n"
        f"Email: {to_email}\n"
        f"Temporary password: {temporary_password}\n\n"
        "Change your password under Account after you sign in.\n\n"
        "If you did not expect this, contact the person who invited you."
    )
    html = f"""
    <p>{greet}</p>
    <p><strong>{inviter_name}</strong> created an Editube account for you to join the workspace
    <strong>{workspace_name}</strong> (role: <strong>{workspace_role}</strong>).</p>
    <p><a href="{login_url}">Sign in to Editube</a></p>
    <p>Email: <strong>{to_email}</strong><br/>
    Temporary password: <code>{temporary_password}</code></p>
    <p>Change your password under Account after you sign in.</p>
    <p>If you did not expect this, contact the person who invited you.</p>
    """
    return send_transactional_email(to_email, subject, text, html)


def send_comment_mention_email(
    to_email: str,
    recipient_name: str | None,
    actor_name: str,
    project_name: str | None,
    video_name: str | None,
    comment_text: str,
    comment_url: str,
) -> bool:
    subject_scope = video_name or project_name or "a video"
    subject = f"You were mentioned in {subject_scope}"
    greet = f"Hi {recipient_name}," if recipient_name else "Hi,"
    preview = (comment_text or "").strip()
    if len(preview) > 220:
        preview = preview[:217].rstrip() + "..."

    text = (
        f"{greet}\n\n"
        f"{actor_name} mentioned you in a comment on Editube.\n\n"
        f"Project: {project_name or 'Unknown project'}\n"
        f"Video: {video_name or 'Unknown video'}\n\n"
        f"Comment:\n\"{preview}\"\n\n"
        f"Open comment: {comment_url}\n\n"
        "You can update mention email preferences in Account settings."
    )
    html = f"""
    <p>{greet}</p>
    <p><strong>{actor_name}</strong> mentioned you in a comment on Editube.</p>
    <p>Project: <strong>{project_name or "Unknown project"}</strong><br/>Video: <strong>{video_name or "Unknown video"}</strong></p>
    <p>Comment:</p>
    <blockquote>{preview}</blockquote>
    <p><a href="{comment_url}">Open comment</a></p>
    <p>You can update mention email preferences in Account settings.</p>
    """
    return send_transactional_email(to_email, subject, text, html)
