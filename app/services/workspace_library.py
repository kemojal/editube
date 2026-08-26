"""The workspace asset library — storage, metadata and the unified feed.

Two kinds of media exist in a workspace and they were never listable together:
`workspace_assets` (logos, LUTs, music, b-roll — the shared library) and
`videos` (cuts, which only ever appear inside the project that owns them).
"Show me everything I have uploaded" therefore had no answer at all.

`library_feed` merges both into one list. Assets are writable from the library
page; project videos are read-only there — deleting a cut is a project-level
act with review consequences, not a file-manager gesture.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.db.models import Project, User, Video, WorkspaceAsset
from app.storage import build_key, get_storage, guess_content_type, storage_available

logger = logging.getLogger(__name__)

ASSET_UPLOAD_SUBDIR = "workspace_assets"

#: User-facing tags on a library asset. The first six are the original brand-kit
#: vocabulary; the rest exist because the library now holds general media, not
#: just the pieces the editor auto-applies.
ALLOWED_ASSET_CATEGORIES = frozenset(
    {
        "logo",
        "lut",
        "music",
        "sfx",
        "lower_third",
        "b_roll",
        "image",
        "video",
        "font",
        "document",
        "other",
    }
)

#: What a category implies when the file itself tells us nothing (legacy rows
#: predate `mime_type`, so a category is often all there is to go on).
_CATEGORY_KIND = {
    "logo": "image",
    "image": "image",
    "lower_third": "image",
    "music": "audio",
    "sfx": "audio",
    "b_roll": "video",
    "video": "video",
}

KINDS = ("video", "image", "audio", "other")

#: Workspace roles allowed to add or remove library files, mirroring the
#: checks on the asset write routes. Anyone else browses read-only, and the
#: feed says so rather than offering a Delete that 403s.
WRITER_ROLES = frozenset({"owner", "producer", "editor"})

#: How many rows per source the feed will scan before merging. Sorting by name
#: or size cannot be pushed into a UNION across two differently-shaped tables,
#: so the merge happens in Python — bounded, and the response says so.
MAX_SCAN_PER_SOURCE = 500


@dataclass(frozen=True)
class StoredAsset:
    """Where an uploaded asset ended up, plus what it turned out to be."""

    file_url: str
    storage_key: Optional[str]
    mime_type: str
    size_bytes: int
    duration_ms: Optional[int]
    width: Optional[int]
    height: Optional[int]


def kind_for(mime_type: Optional[str], category: Optional[str], file_url: Optional[str]) -> str:
    """`video` | `image` | `audio` | `other`, from the strongest signal available."""
    mime = (mime_type or "").lower()
    if not mime and file_url:
        mime = (mimetypes.guess_type(file_url)[0] or "").lower()
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    return _CATEGORY_KIND.get((category or "").lower(), "other")


def category_for_mime(mime_type: Optional[str]) -> str:
    """Default tag for an upload that did not pick one."""
    mime = (mime_type or "").lower()
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "music"
    return "other"


def probe_media(path: str) -> dict:
    """Duration/dimensions via ffprobe. Never fatal — an unprobeable file is
    still a perfectly good asset, it just shows no duration chip."""
    from app.services.ingest_service import probe_media_file

    try:
        return probe_media_file(path) or {}
    except Exception:  # noqa: BLE001
        logger.warning("probe failed for %s", path, exc_info=True)
        return {}


def store_asset_file(
    path: str,
    *,
    filename: Optional[str],
    content_type: Optional[str],
) -> StoredAsset:
    """Put a finished temp file where it belongs and describe it.

    Prefers the configured object store (R2/Cloudinary) so the URL works from
    any process; falls back to local disk when the backend is not configured,
    which is what self-hosted and dev installs run on.
    """
    mime = (content_type or "").split(";")[0].strip() or guess_content_type(
        filename, resource_type="raw", fallback="application/octet-stream"
    )
    size_bytes = os.path.getsize(path) if os.path.isfile(path) else 0

    probed = probe_media(path)
    duration_s = probed.get("duration")
    width = probed.get("width") or None
    height = probed.get("height") or None
    duration_ms = int(duration_s * 1000) if duration_s else None

    if storage_available():
        key = build_key(
            folder=ASSET_UPLOAD_SUBDIR,
            filename=filename,
            content_type=mime,
        )
        result = get_storage().upload_path(path, key=key, content_type=mime)
        return StoredAsset(
            file_url=result.url,
            storage_key=result.key,
            mime_type=result.content_type or mime,
            size_bytes=int(result.bytes or size_bytes),
            duration_ms=duration_ms,
            width=width,
            height=height,
        )

    from app.utils.storage import UPLOAD_DIRECTORY

    sub = os.path.join(UPLOAD_DIRECTORY, ASSET_UPLOAD_SUBDIR)
    os.makedirs(sub, exist_ok=True)
    ext = os.path.splitext(filename or "")[1] or mimetypes.guess_extension(mime) or ""
    dest = os.path.join(sub, f"{uuid.uuid4().hex}{ext}")
    os.replace(path, dest)
    return StoredAsset(
        file_url=dest,
        storage_key=None,
        mime_type=mime,
        size_bytes=size_bytes,
        duration_ms=duration_ms,
        width=width,
        height=height,
    )


def is_remote(file_url: Optional[str]) -> bool:
    return bool(file_url) and file_url.lower().startswith(("http://", "https://"))


def asset_payload(a: WorkspaceAsset, *, workspace_id: int) -> dict:
    """One library asset, in the shape both `/assets` and the feed return."""
    return {
        "id": a.id,
        "category": a.category,
        "title": a.title,
        "file_url": a.file_url if is_remote(a.file_url) else None,
        "media_url": f"/workspaces/{workspace_id}/assets/{a.id}/media",
        "mime_type": a.mime_type,
        "size_bytes": int(a.size_bytes or 0),
        "duration_ms": a.duration_ms,
        "width": a.width,
        "height": a.height,
        "thumbnail_url": a.thumbnail_url,
        "extra": a.extra,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


def _user_ref(user: Optional[User]) -> Optional[dict]:
    if not user:
        return None
    return {
        "id": user.id,
        "name": (user.full_name or user.name or "").strip() or None,
        "avatar_url": getattr(user, "avatar_url", None),
    }


def _asset_item(a: WorkspaceAsset, *, workspace_id: int, can_write: bool) -> dict:
    payload = asset_payload(a, workspace_id=workspace_id)
    return {
        "uid": f"asset-{a.id}",
        "id": a.id,
        "source": "library",
        "kind": kind_for(a.mime_type, a.category, a.file_url),
        "title": a.title,
        "url": payload["file_url"],
        "media_url": payload["media_url"],
        "thumbnail_url": a.thumbnail_url,
        "mime_type": a.mime_type,
        "size_bytes": int(a.size_bytes or 0),
        "duration_ms": a.duration_ms,
        "width": a.width,
        "height": a.height,
        "category": a.category,
        "project": None,
        "uploaded_by": _user_ref(a.created_by),
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "can_delete": can_write,
    }


def _video_item(v: Video) -> dict:
    return {
        "uid": f"video-{v.id}",
        "id": v.id,
        "source": "project",
        "kind": "video",
        "title": v.name or f"Cut {v.id}",
        "url": v.file_path if is_remote(v.file_path) else None,
        "media_url": None,
        "thumbnail_url": v.thumbnail_url,
        "mime_type": mimetypes.guess_type(v.file_path or "")[0] or "video/mp4",
        "size_bytes": int(v.size_bytes or 0),
        "duration_ms": int(v.duration * 1000) if v.duration else None,
        "width": None,
        "height": None,
        "category": None,
        "project": {"id": v.project_id, "name": v.project.name if v.project else None},
        "uploaded_by": _user_ref(v.uploader),
        "created_at": v.created_at.isoformat() if v.created_at else None,
        # Deleting a cut carries review consequences (versions, sign-offs), so
        # the library links to the project rather than offering a delete.
        "can_delete": False,
    }


def _sort_key(sort: str):
    if sort == "name":
        return (lambda item: (item["title"] or "").lower()), False
    if sort == "size":
        return (lambda item: int(item["size_bytes"] or 0)), True
    return (lambda item: (item["created_at"] or "")), True


def library_feed(
    db: Session,
    *,
    workspace_id: int,
    member_role: str = "",
    kind: str = "all",
    source: str = "all",
    q: str = "",
    sort: str = "recent",
    limit: int = 48,
    offset: int = 0,
) -> dict:
    """Library assets + project videos as one paginated, filtered list."""
    term = (q or "").strip()
    can_write = (member_role or "").lower() in WRITER_ROLES
    items: list[dict] = []
    truncated = False

    if source in ("all", "library"):
        query = (
            db.query(WorkspaceAsset)
            .options(joinedload(WorkspaceAsset.created_by))
            .filter(WorkspaceAsset.workspace_id == workspace_id)
        )
        if term:
            query = query.filter(WorkspaceAsset.title.ilike(f"%{term}%"))
        rows = (
            query.order_by(WorkspaceAsset.created_at.desc())
            .limit(MAX_SCAN_PER_SOURCE + 1)
            .all()
        )
        truncated = truncated or len(rows) > MAX_SCAN_PER_SOURCE
        items.extend(
            _asset_item(a, workspace_id=workspace_id, can_write=can_write)
            for a in rows[:MAX_SCAN_PER_SOURCE]
        )

    if source in ("all", "project"):
        query = (
            db.query(Video)
            .join(Project, Project.id == Video.project_id)
            .options(joinedload(Video.project), joinedload(Video.uploader))
            .filter(Project.workspace_id == workspace_id)
        )
        if term:
            query = query.filter(
                or_(Video.name.ilike(f"%{term}%"), Project.name.ilike(f"%{term}%"))
            )
        rows = (
            query.order_by(Video.created_at.desc()).limit(MAX_SCAN_PER_SOURCE + 1).all()
        )
        truncated = truncated or len(rows) > MAX_SCAN_PER_SOURCE
        items.extend(_video_item(v) for v in rows[:MAX_SCAN_PER_SOURCE])

    counts = {k: 0 for k in KINDS}
    for item in items:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
    counts["all"] = len(items)

    if kind in KINDS:
        items = [item for item in items if item["kind"] == kind]

    key, reverse = _sort_key(sort)
    items.sort(key=key, reverse=reverse)

    total = len(items)
    page = items[offset : offset + limit]
    return {
        "items": page,
        "total": total,
        "limit": limit,
        "offset": offset,
        "counts": counts,
        # The UI says "showing the most recent N" rather than implying the list
        # is everything the workspace holds.
        "truncated": truncated,
        "scan_limit": MAX_SCAN_PER_SOURCE,
    }
