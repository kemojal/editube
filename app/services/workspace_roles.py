"""Allowed workspace member / invite role strings (agency hierarchy)."""

from __future__ import annotations

from fastapi import HTTPException

# Owner is assigned via workspace.owner_user_id + membership row; not inviteable.
WORKSPACE_INVITE_ROLES = frozenset({"producer", "editor", "assistant", "client", "guest"})


def normalize_invite_role(raw: str | None) -> str:
    r = (raw or "editor").strip().lower()
    if r not in WORKSPACE_INVITE_ROLES:
        raise HTTPException(status_code=400, detail="Invalid workspace role for invite")
    return r
