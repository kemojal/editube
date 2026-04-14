from __future__ import annotations

import re
from typing import Iterable

from app.db.models import User

_MENTION_PATTERN = re.compile(r"(?<![\w@])@([A-Za-z0-9._-]{2,64})")


def extract_mention_handles(text: str) -> list[str]:
    if not text:
        return []
    handles: list[str] = []
    seen: set[str] = set()
    for match in _MENTION_PATTERN.finditer(text):
        normalized = match.group(1).strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            handles.append(normalized)
    return handles


def _normalize_name_handle(name: str) -> str:
    return re.sub(r"[^a-z0-9._-]", "", name.strip().lower())


def user_mention_handles(user: User) -> set[str]:
    handles: set[str] = set()
    if user.name:
        normalized_name = _normalize_name_handle(user.name)
        if normalized_name:
            handles.add(normalized_name)
        for part in user.name.split():
            normalized_part = _normalize_name_handle(part)
            if normalized_part:
                handles.add(normalized_part)
    if user.email and "@" in user.email:
        local = user.email.split("@", 1)[0].strip().lower()
        local = re.sub(r"[^a-z0-9._-]", "", local)
        if local:
            handles.add(local)
    return handles


def resolve_mentioned_users(
    mention_handles: Iterable[str],
    candidate_users: Iterable[User],
    actor_user_id: int | None = None,
) -> list[User]:
    mention_set = {h.strip().lower() for h in mention_handles if h and h.strip()}
    if not mention_set:
        return []

    resolved: list[User] = []
    seen_ids: set[int] = set()
    for user in candidate_users:
        if actor_user_id is not None and user.id == actor_user_id:
            continue
        if user.id in seen_ids:
            continue
        if user_mention_handles(user) & mention_set:
            seen_ids.add(user.id)
            resolved.append(user)
    return resolved
