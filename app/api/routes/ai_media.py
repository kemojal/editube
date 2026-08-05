"""AI media generation endpoints — images and video as project assets.

The media panel drives these: create a job, poll it while a pending tile shows
progress, then drag the finished asset onto the timeline. Bytes land in object
storage (R2); this module owns the DB rows and the access checks.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import GeneratedMedia, Project, User, Video
from app.services.project_access import get_project_for_user
from app.utils.security import get_current_user

router = APIRouter(prefix="/projects", tags=["ai-media"])

ALLOWED_KINDS = {"image", "video"}
ALLOWED_ASPECTS = {"auto", "16:9", "9:16", "1:1", "4:3", "3:4", "21:9"}
#: Model ids this server can actually run, and the provider behind each. A model
#: the registry offers but that has no adapter here is rejected with a clear
#: message rather than failing deep inside a worker.
IMPLEMENTED_MODELS: dict[str, str] = {
    # Google, direct
    "veo-3.1-generate-preview": "gemini",
    "veo-3.1-lite-generate-preview": "gemini",
    "gemini-omni-flash-video": "gemini",
    "gemini-3.1-flash-image-preview": "gemini",
    "gemini-3-pro-image": "gemini",
    # OpenRouter (image only — it has no video-generation surface).
    # Ids verified against GET /api/v1/models with image output modality.
    "google/gemini-3.1-flash-image": "openrouter",
    "google/gemini-3.1-flash-lite-image": "openrouter",
    "google/gemini-3-pro-image-preview": "openrouter",
    "openai/gpt-5-image": "openrouter",
    "openai/gpt-5-image-mini": "openrouter",
}

#: Models the product offers but cannot execute here yet. They are surfaced in
#: the pickers as unavailable so the roadmap is visible, and generation requests
#: against them are still rejected by the IMPLEMENTED_MODELS check.
#:
#: Seedance sits here rather than under OpenRouter on purpose: as of the last
#: check, GET https://openrouter.ai/api/v1/models returns **no** model with a
#: `video` output modality and no Seedance entry at all, so routing it through
#: OpenRouter is not currently possible. It needs a direct ByteDance/Volcengine
#: Ark adapter (provider `seedance`) plus a key.
PLANNED_MODELS: dict[str, str] = {
    "seedance-2.0": "seedance",
    "seedance-2.0-mini": "seedance",
    "seedance-2.0-fast": "seedance",
}
#: Widest window any implemented video model supports; per-model limits are
#: enforced by the registry on the client.
MIN_DURATION = 3.0
MAX_DURATION = 15.0


class ReferenceImage(BaseModel):
    mime_type: str
    data_base64: str


class GenerateMediaBody(BaseModel):
    kind: str = Field(..., description="image | video")
    references: list[ReferenceImage] = Field(default_factory=list, max_length=4)
    prompt: str
    model: str | None = None
    aspect_ratio: str | None = None
    duration_seconds: float | None = None
    video_id: int | None = None


def _serialize(row: GeneratedMedia) -> dict:
    return {
        "id": row.id,
        "project_id": row.project_id,
        "video_id": row.video_id,
        "kind": row.kind,
        "prompt": row.prompt,
        "model": row.model,
        "aspect_ratio": row.aspect_ratio,
        "reference_urls": list(row.reference_urls or []),
        "duration_seconds": row.duration_seconds,
        "status": row.status,
        "saved": row.saved,
        "progress": row.progress,
        "error_message": row.error_message,
        "url": row.url,
        "thumbnail_url": row.thumbnail_url,
        "mime_type": row.mime_type,
        "width": row.width,
        "height": row.height,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _project_or_403(project_id: int, db: Session, user: User) -> Project:
    """404/403 via the shared access helper, so this matches every other route."""
    return get_project_for_user(db, project_id, user)


def _media_or_404(project_id: int, media_id: int, db: Session, user: User) -> GeneratedMedia:
    _project_or_403(project_id, db, user)
    row = (
        db.query(GeneratedMedia)
        .filter(GeneratedMedia.id == media_id, GeneratedMedia.project_id == project_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Generated media not found")
    return row


@router.get("/{project_id}/ai/media/providers")
def list_media_providers(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Which generation providers this server can call.

    The client's model picker keys availability off this rather than a
    hardcoded list, so adding a key to the server is all it takes to enable a
    provider in the UI.
    """
    _project_or_403(project_id, db, current_user)
    from app.services.ai_media import provider_availability

    available = provider_availability()
    return {
        "providers": available,
        "models": {
            model: {"provider": provider, "available": bool(available.get(provider))}
            for model, provider in IMPLEMENTED_MODELS.items()
        },
    }


@router.get("/{project_id}/ai/media")
def list_generated_media(
    project_id: int,
    saved: bool | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List generations. ``saved=true`` returns only reviewed-and-kept media."""
    _project_or_403(project_id, db, current_user)
    query = db.query(GeneratedMedia).filter(GeneratedMedia.project_id == project_id)
    if saved is not None:
        query = query.filter(GeneratedMedia.saved.is_(saved))
    rows = (
        query
        .order_by(GeneratedMedia.created_at.desc())
        .all()
    )
    return [_serialize(row) for row in rows]


@router.post("/{project_id}/ai/media")
def create_generated_media(
    project_id: int,
    body: GenerateMediaBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _project_or_403(project_id, db, current_user)

    kind = (body.kind or "").strip().lower()
    if kind not in ALLOWED_KINDS:
        raise HTTPException(status_code=400, detail="kind must be 'image' or 'video'")
    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    if body.aspect_ratio and body.aspect_ratio not in ALLOWED_ASPECTS:
        raise HTTPException(status_code=400, detail=f"aspect_ratio must be one of {sorted(ALLOWED_ASPECTS)}")

    duration = body.duration_seconds
    if kind == "video":
        duration = float(duration or 6.0)
        if not (MIN_DURATION <= duration <= MAX_DURATION):
            raise HTTPException(
                status_code=400,
                detail=f"duration_seconds must be between {MIN_DURATION:g} and {MAX_DURATION:g}",
            )
    else:
        duration = None

    model = (body.model or "").strip() or None
    if model:
        provider = IMPLEMENTED_MODELS.get(model)
        if provider is None:
            # 501, not 400: the request is well-formed, this deployment just has
            # no adapter for that provider yet.
            raise HTTPException(
                status_code=501,
                detail=f"Model {model!r} is not available on this server yet",
            )
        from app.services.ai_media import provider_availability

        if not provider_availability().get(provider):
            raise HTTPException(
                status_code=503,
                detail=f"{provider} is not configured on this server (missing API key)",
            )
        if kind == "video" and provider == "openrouter":
            raise HTTPException(
                status_code=400,
                detail="OpenRouter has no video-generation models; pick a video model.",
            )

    video_id = body.video_id
    if video_id is not None:
        owns_video = (
            db.query(Video)
            .filter(Video.id == video_id, Video.project_id == project_id)
            .first()
        )
        if owns_video is None:
            # Provenance only — a mismatched id is dropped rather than rejected,
            # so a stale editor session cannot block generation.
            video_id = None

    # Reference images are uploaded once and referenced by URL: the row stays
    # small and the worker re-fetches them instead of carrying base64 through
    # the queue.
    reference_urls: list[str] = []
    if body.references:
        import base64 as _b64

        from app.storage import build_key, get_storage, storage_available

        if not storage_available():
            raise HTTPException(status_code=503, detail="Object storage is not configured")
        for index, reference in enumerate(body.references):
            try:
                raw = _b64.b64decode(reference.data_base64)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail="Reference image is not valid base64") from exc
            if len(raw) > 8 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="Reference images must be under 8 MB")
            key = build_key(
                folder=f"generated/{project_id}/refs",
                public_id=f"{current_user.id}_{index}_{abs(hash(reference.data_base64[:64]))}",
                content_type=reference.mime_type,
            )
            reference_urls.append(
                get_storage().upload_bytes(raw, key=key, content_type=reference.mime_type).url
            )

    row = GeneratedMedia(
        project_id=project_id,
        video_id=video_id,
        user_id=current_user.id,
        kind=kind,
        prompt=prompt,
        model=model,
        aspect_ratio=(body.aspect_ratio or None),
        duration_seconds=duration,
        status="pending",
        progress=0,
        reference_urls=reference_urls,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    from app.jobs.queue import enqueue_generated_media_job

    if enqueue_generated_media_job(row.id) is None:
        row.status = "failed"
        row.error_message = (
            "Background worker unavailable. Configure REDIS_URL and run an RQ worker."
        )
        db.commit()
        db.refresh(row)

    return _serialize(row)


@router.get("/{project_id}/ai/media/{media_id}")
def get_generated_media(
    project_id: int,
    media_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _serialize(_media_or_404(project_id, media_id, db, current_user))


@router.post("/{project_id}/ai/media/{media_id}/cancel")
def cancel_generated_media(
    project_id: int,
    media_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = _media_or_404(project_id, media_id, db, current_user)
    if row.status in {"ready", "failed", "cancelled"}:
        return _serialize(row)
    # The worker checks this between polls; it cannot be interrupted mid-request.
    row.cancel_requested = True
    db.commit()
    db.refresh(row)
    return _serialize(row)


@router.post("/{project_id}/ai/media/{media_id}/save")
def save_generated_media(
    project_id: int,
    media_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Accept a reviewed generation into the project's media."""
    row = _media_or_404(project_id, media_id, db, current_user)
    if row.status != "ready" or not row.url:
        raise HTTPException(status_code=409, detail="Only a finished generation can be saved")
    row.saved = True
    db.commit()
    db.refresh(row)
    return _serialize(row)


@router.delete("/{project_id}/ai/media/{media_id}")
def delete_generated_media(
    project_id: int,
    media_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = _media_or_404(project_id, media_id, db, current_user)
    storage_key = row.storage_key
    db.delete(row)
    db.commit()

    if storage_key:
        try:
            from app.storage import get_storage

            backend = get_storage()
            delete = getattr(backend, "delete", None)
            if callable(delete):
                delete(storage_key)
        except Exception:  # noqa: BLE001 - the row is gone; an orphan object is not worth a 500
            pass
    return {"ok": True, "id": media_id}
