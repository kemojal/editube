"""NLE comment ↔ marker synchronisation service.

Handles bidirectional sync between Editube comments and NLE (Premiere,
Resolve, FCP X, After Effects) markers via the common MarkerItem format.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional, Sequence

from sqlalchemy.orm import Session

from app.db.models import Comment, NLESession, Video
from app.services.comment_export import (
    _author,
    _flat_comments_in_order,
)

logger = logging.getLogger(__name__)


# ── Export ─────────────────────────────────────────────────────────────


def export_markers(
    db: Session,
    video_id: int,
    *,
    include_replies: bool = False,
) -> list[dict]:
    """Export top-level comments (optionally including replies) as marker dicts.

    Each dict has the shape of ``MarkerItem`` from the Pydantic schemas.
    """
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise ValueError(f"Video {video_id} not found")

    comments: Sequence[Comment] = (
        db.query(Comment).filter(Comment.video_id == video_id).all()
    )
    ordered = _flat_comments_in_order(comments)

    markers: list[dict] = []
    for c in ordered:
        if not include_replies and c.parent_id is not None:
            continue
        markers.append(
            {
                "timecode_sec": c.timecode or 0,
                "end_timecode_sec": c.end_timecode,
                "text": (c.text or "").strip(),
                "author": _author(c),
                "color": None,
                "kind": getattr(c, "kind", "comment") or "comment",
                "status": getattr(c, "status", "open") or "open",
                "editube_comment_id": c.id,
            }
        )
    return markers


# ── Import ─────────────────────────────────────────────────────────────


def import_markers(
    db: Session,
    video_id: int,
    user_id: int,
    markers: list[dict],
    source_nle: str,
    *,
    replace_existing: bool = False,
) -> dict:
    """Import marker list from an NLE into Editube comments.

    ``markers`` is a list of dicts matching ``MarkerItem`` shape.

    Returns ``{"created": int, "updated": int, "skipped": int, "total": int}``.
    """
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise ValueError(f"Video {video_id} not found")

    if replace_existing:
        # Delete existing NLE-sourced root comments for a clean slate
        db.query(Comment).filter(
            Comment.video_id == video_id,
            Comment.parent_id.is_(None),
        ).delete(synchronize_session="fetch")
        db.flush()

    existing_comments: list[Comment] = (
        db.query(Comment)
        .filter(Comment.video_id == video_id, Comment.parent_id.is_(None))
        .all()
    )
    existing_index = {
        (c.timecode, (c.text or "").strip()[:120]): c for c in existing_comments
    }

    created = updated = skipped = 0

    for m in markers:
        tc = int(m.get("timecode_sec") or 0)
        text = (m.get("text") or "").strip()
        if not text:
            skipped += 1
            continue

        key = (tc, text[:120])
        existing = None

        # First try to match by editube_comment_id (round-trip)
        eid = m.get("editube_comment_id")
        if eid:
            existing = db.query(Comment).filter(Comment.id == eid, Comment.video_id == video_id).first()

        # Fallback: match by timecode + text prefix
        if not existing:
            existing = existing_index.get(key)

        if existing:
            # Update existing comment if text changed
            if existing.text != text or existing.timecode != tc:
                existing.text = text
                existing.timecode = tc
                existing.end_timecode = m.get("end_timecode_sec")
                if m.get("kind"):
                    existing.kind = m["kind"]
                updated += 1
            else:
                skipped += 1
        else:
            comment = Comment(
                video_id=video_id,
                user_id=user_id,
                text=text,
                timecode=tc,
                end_timecode=m.get("end_timecode_sec"),
                kind=m.get("kind", "comment"),
                status=m.get("status", "open"),
            )
            db.add(comment)
            created += 1

    db.commit()

    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "total": len(markers),
    }


# ── Diff ──────────────────────────────────────────────────────────────


def diff_markers(
    db: Session,
    video_id: int,
    since: datetime,
) -> dict:
    """Return comments added/changed since ``since`` as marker dicts.

    Returns ``{"added": [...], "removed": [], "changed": [...]}``.
    (Removals aren't tracked yet — we'd need soft-delete for that.)
    """
    comments = (
        db.query(Comment)
        .filter(
            Comment.video_id == video_id,
            Comment.parent_id.is_(None),
            Comment.updated_at >= since,
        )
        .all()
    )

    added: list[dict] = []
    changed: list[dict] = []

    for c in comments:
        item = {
            "timecode_sec": c.timecode or 0,
            "end_timecode_sec": c.end_timecode,
            "text": (c.text or "").strip(),
            "author": _author(c),
            "kind": getattr(c, "kind", "comment") or "comment",
            "status": getattr(c, "status", "open") or "open",
            "editube_comment_id": c.id,
        }
        if c.created_at and c.created_at >= since:
            added.append(item)
        else:
            changed.append(item)

    return {"added": added, "removed": [], "changed": changed}


# ── Session management ────────────────────────────────────────────────


def touch_nle_session(db: Session, session_id: int) -> None:
    """Update last_sync_at for an NLE session."""
    session = db.query(NLESession).filter(NLESession.id == session_id).first()
    if session:
        session.last_sync_at = datetime.now(timezone.utc)
        db.commit()
