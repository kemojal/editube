"""Create a personal workspace + owner membership when missing."""

from __future__ import annotations

import secrets

from sqlalchemy.orm import Session

from app.db.models import User, UserSettings, Workspace, WorkspaceBranding, WorkspaceMember


def _slug_for_user(user_id: int) -> str:
    return f"ws-{user_id}-{secrets.token_hex(4)}"


def ensure_personal_workspace(db: Session, user: User) -> Workspace:
    # Serialize concurrent bootstraps for this user before the existence check.
    # The account dialog fires `/workspaces` and `/billing/usage` at the same
    # time and both self-heal now, so without the lock two requests can each
    # see "no workspace" and each create one. Postgres only — SQLite (tests)
    # has no row locks and no concurrency to protect against.
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.query(User).filter(User.id == user.id).with_for_update().first()

    existing = (
        db.query(WorkspaceMember)
        .filter(WorkspaceMember.user_id == user.id, WorkspaceMember.role == "owner")
        .first()
    )
    if existing:
        ws = db.query(Workspace).filter(Workspace.id == existing.workspace_id).first()
        if ws:
            return ws

    settings = db.query(UserSettings).filter(UserSettings.user_id == user.id).first()
    name = (settings.workspace_name if settings else None) or "My Workspace"
    ws = Workspace(name=name, slug=_slug_for_user(user.id), owner_user_id=user.id)
    db.add(ws)
    db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="owner"))
    db.add(WorkspaceBranding(workspace_id=ws.id))
    db.commit()
    db.refresh(ws)
    return ws
