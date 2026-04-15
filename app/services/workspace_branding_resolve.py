"""Resolve workspace branding for client-facing pages."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import Project, WorkspaceBranding


def branding_public_dict(db: Session, project: Project | None) -> dict | None:
    if not project or not project.workspace_id:
        return None
    b = (
        db.query(WorkspaceBranding)
        .filter(WorkspaceBranding.workspace_id == project.workspace_id)
        .first()
    )
    if not b:
        return None
    return {
        "logo_url": b.logo_url,
        "primary_color": b.primary_color,
        "accent_color": b.accent_color,
        "client_footer_text": b.client_footer_text,
    }


def branding_by_host(db: Session, host: str) -> dict | None:
    h = (host or "").strip().lower()
    if ":" in h:
        h = h.split(":", 1)[0]
    if not h:
        return None
    b = db.query(WorkspaceBranding).filter(WorkspaceBranding.custom_domain == h).first()
    if not b or not b.domain_verified_at:
        return None
    return {
        "workspace_id": b.workspace_id,
        "logo_url": b.logo_url,
        "primary_color": b.primary_color,
        "accent_color": b.accent_color,
        "client_footer_text": b.client_footer_text,
    }
