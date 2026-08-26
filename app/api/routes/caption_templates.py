"""Caption template catalogue for the rough-cut editor.

Reads are available to every authenticated user — the editor's template
gallery loads from here (falling back to its bundled catalogue when the table
is empty). Writes are internal-only: refine/polish existing templates, create
new ones, archive or delete them, and seed the frontend's built-ins.

Who counts as internal
----------------------
- ``users.role == "admin"``, or
- an email listed in the ``CAPTION_TEMPLATE_EDITORS`` env var (comma-separated,
  case-insensitive). ``CAPTION_TEMPLATE_EDITORS=*`` opens editing to every
  authenticated user — intended for local development only.

The ``patch`` payload is the frontend's ``Partial<CaptionStyle>``. The backend
stores it as opaque JSON; the frontend's ``migrateCaptionStyle`` clamps and
sanitises every field on read, so a malformed patch can degrade one template's
look but can never break the editor.
"""

import os
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.models import CaptionTemplateDef, User
from ...db.database import get_db
from ...utils.security import get_current_user

router = APIRouter(prefix="/caption-templates", tags=["Caption Templates"])

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
_CATEGORIES = {"new", "trend", "emoji", "premium", "speakers", "signature"}


def can_edit_caption_templates(user: User) -> bool:
    if (user.role or "").strip().lower() == "admin":
        return True
    raw = os.getenv("CAPTION_TEMPLATE_EDITORS", "")
    entries = {entry.strip().lower() for entry in raw.split(",") if entry.strip()}
    if "*" in entries:
        return True
    email = (user.email or "").strip().lower()
    return bool(email) and email in entries


def _require_editor(user: User) -> None:
    if not can_edit_caption_templates(user):
        raise HTTPException(status_code=403, detail="Caption template editing is internal-only")


def _normalize_slug(raw: str) -> str:
    slug = (raw or "").strip().lower()
    if not _SLUG_RE.match(slug):
        raise HTTPException(
            status_code=400,
            detail="Slug must be 1-80 chars of lowercase letters, digits and hyphens",
        )
    return slug


def _normalize_category(raw: str) -> str:
    category = (raw or "").strip().lower()
    if category not in _CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"Category must be one of: {', '.join(sorted(_CATEGORIES))}",
        )
    return category


def _normalize_label(raw: str) -> str:
    label = (raw or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="Label is required")
    return label[:120]


class CaptionTemplateOut(BaseModel):
    slug: str
    category: str
    label: str
    sample: str
    tag: str | None
    blurb: str
    patch: dict
    sort_order: int
    archived: bool
    builtin: bool
    updated_at: datetime

    model_config = {"from_attributes": True}


class CaptionTemplateListResponse(BaseModel):
    can_edit: bool
    templates: list[CaptionTemplateOut]


class CaptionTemplateCreate(BaseModel):
    slug: str
    category: str = "new"
    label: str
    sample: str = ""
    tag: str | None = None
    blurb: str = ""
    patch: dict = Field(default_factory=dict)
    sort_order: int = 0


class CaptionTemplateUpdate(BaseModel):
    category: str | None = None
    label: str | None = None
    sample: str | None = None
    tag: str | None = None
    blurb: str | None = None
    patch: dict | None = None
    sort_order: int | None = None
    archived: bool | None = None


class CaptionTemplateSyncRequest(BaseModel):
    templates: list[CaptionTemplateCreate]


class CaptionTemplateSyncResponse(BaseModel):
    inserted: int
    skipped: int


@router.get("", response_model=CaptionTemplateListResponse)
def list_caption_templates(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    can_edit = can_edit_caption_templates(current_user)
    query = db.query(CaptionTemplateDef)
    if not can_edit:
        # Archived templates leave the gallery for everyone else, but drafts
        # that already reference them keep resolving client-side.
        query = query.filter(CaptionTemplateDef.archived.is_(False))
    rows = query.order_by(CaptionTemplateDef.sort_order, CaptionTemplateDef.id).all()
    return CaptionTemplateListResponse(
        can_edit=can_edit,
        templates=[CaptionTemplateOut.model_validate(row) for row in rows],
    )


@router.post("", response_model=CaptionTemplateOut, status_code=201)
def create_caption_template(
    data: CaptionTemplateCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_editor(current_user)
    slug = _normalize_slug(data.slug)
    existing = db.query(CaptionTemplateDef).filter(CaptionTemplateDef.slug == slug).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"A template with slug '{slug}' already exists")
    row = CaptionTemplateDef(
        slug=slug,
        category=_normalize_category(data.category),
        label=_normalize_label(data.label),
        sample=(data.sample or "").strip()[:200],
        tag=(data.tag or "").strip()[:40] or None,
        blurb=(data.blurb or "").strip(),
        patch=data.patch or {},
        sort_order=data.sort_order,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.put("/{slug}", response_model=CaptionTemplateOut)
def update_caption_template(
    slug: str,
    data: CaptionTemplateUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_editor(current_user)
    row = (
        db.query(CaptionTemplateDef)
        .filter(CaptionTemplateDef.slug == _normalize_slug(slug))
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Template not found")

    fields = data.model_dump(exclude_unset=True)
    if "category" in fields:
        row.category = _normalize_category(fields["category"])
    if "label" in fields:
        row.label = _normalize_label(fields["label"])
    if "sample" in fields:
        row.sample = (fields["sample"] or "").strip()[:200]
    if "tag" in fields:
        row.tag = (fields["tag"] or "").strip()[:40] or None
    if "blurb" in fields:
        row.blurb = (fields["blurb"] or "").strip()
    if "patch" in fields:
        if not isinstance(fields["patch"], dict):
            raise HTTPException(status_code=400, detail="patch must be an object")
        row.patch = fields["patch"]
    if "sort_order" in fields and fields["sort_order"] is not None:
        row.sort_order = int(fields["sort_order"])
    if "archived" in fields and fields["archived"] is not None:
        row.archived = bool(fields["archived"])

    db.commit()
    db.refresh(row)
    return row


@router.delete("/{slug}", status_code=204)
def delete_caption_template(
    slug: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Hard delete. Prefer archiving — drafts keep the applied style either way
    (it is copied into the draft on apply), but a deleted slug can no longer be
    re-applied from the gallery."""
    _require_editor(current_user)
    deleted = (
        db.query(CaptionTemplateDef)
        .filter(CaptionTemplateDef.slug == _normalize_slug(slug))
        .delete(synchronize_session=False)
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Template not found")
    db.commit()


@router.post("/sync-builtins", response_model=CaptionTemplateSyncResponse)
def sync_builtin_caption_templates(
    data: CaptionTemplateSyncRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Seed the table from the frontend's bundled catalogue.

    Insert-only: slugs that already exist are skipped so a re-sync never
    clobbers a template someone has polished in the editor.
    """
    _require_editor(current_user)
    existing = {slug for (slug,) in db.query(CaptionTemplateDef.slug).all()}
    inserted = 0
    skipped = 0
    for index, template in enumerate(data.templates):
        slug = _normalize_slug(template.slug)
        if slug in existing:
            skipped += 1
            continue
        db.add(
            CaptionTemplateDef(
                slug=slug,
                category=_normalize_category(template.category),
                label=_normalize_label(template.label),
                sample=(template.sample or "").strip()[:200],
                tag=(template.tag or "").strip()[:40] or None,
                blurb=(template.blurb or "").strip(),
                patch=template.patch or {},
                sort_order=template.sort_order if template.sort_order else index,
                builtin=True,
            )
        )
        existing.add(slug)
        inserted += 1
    db.commit()
    return CaptionTemplateSyncResponse(inserted=inserted, skipped=skipped)
