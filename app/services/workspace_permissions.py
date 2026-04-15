"""Workspace-level role checks for invites and admin actions."""

from __future__ import annotations

WORKSPACE_ROLES_MANAGE_MEMBERS = frozenset({"owner", "producer"})
WORKSPACE_ROLES_EDIT_BRANDING = frozenset({"owner", "producer"})


def can_manage_workspace_members(role: str) -> bool:
    return role in WORKSPACE_ROLES_MANAGE_MEMBERS


def can_edit_workspace_branding(role: str) -> bool:
    return role in WORKSPACE_ROLES_EDIT_BRANDING
