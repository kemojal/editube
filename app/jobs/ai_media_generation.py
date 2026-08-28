"""RQ job: generate an image or video and store it as a project asset.

The row in ``generated_media`` already exists when this runs (the API creates it
so the media panel can show a pending tile immediately). This job walks it
through ``running`` → ``ready``/``failed``, writing progress as it goes so the
frontend's poll has something to render.
"""

from __future__ import annotations

import logging
import mimetypes

from app.db.database import SessionLocal
from app.db.models import GeneratedMedia
from app.services.ai_media import GenerationCancelled, generate_image, generate_video
from app.storage import build_key, get_storage, storage_available

logger = logging.getLogger(__name__)


def _extension_for(mime_type: str, kind: str) -> str:
    guessed = mimetypes.guess_extension(mime_type or "") or ""
    if guessed:
        return guessed.lstrip(".")
    return "mp4" if kind == "video" else "png"


def _load_references(urls: list[str]) -> list[dict[str, str]]:
    """Fetch reference images back as inline data for the model call."""
    import base64

    import requests

    loaded: list[dict[str, str]] = []
    for url in urls[:4]:
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            loaded.append(
                {
                    "mime_type": response.headers.get("content-type", "image/png"),
                    "data_base64": base64.b64encode(response.content).decode(),
                }
            )
        except Exception:  # noqa: BLE001 - a missing reference must not kill the job
            logger.warning("Could not load reference image %s", url)
    return loaded


def generate_media_job(media_id: int) -> dict[str, object]:
    """Entry point registered with RQ. Returns a small result summary."""
    db = SessionLocal()
    try:
        row = db.query(GeneratedMedia).filter(GeneratedMedia.id == media_id).first()
        if row is None:
            raise RuntimeError(f"Generated media {media_id} was removed before processing")
        if row.cancel_requested:
            row.status = "cancelled"
            db.commit()
            return {"status": "cancelled", "id": media_id}

        row.status = "running"
        row.progress = 1
        db.commit()

        def on_progress(percent: int, _stage: str) -> None:
            # Committed on every step: the tile's progress bar is the only
            # feedback a user gets during a multi-minute Veo job.
            row.progress = max(0, min(99, int(percent)))
            db.commit()

        def is_cancelled() -> bool:
            db.refresh(row)
            return bool(row.cancel_requested)

        references = _load_references(list(row.reference_urls or []))
        reference = references[0] if references else None

        if row.kind == "video":
            result = generate_video(
                prompt=row.prompt,
                model=row.model,
                aspect_ratio=row.aspect_ratio,
                duration_seconds=row.duration_seconds,
                reference=reference,
                on_progress=on_progress,
                is_cancelled=is_cancelled,
            )
        else:
            from app.api.routes.ai_media import IMPLEMENTED_MODELS

            result = generate_image(
                prompt=row.prompt,
                model=row.model,
                provider=IMPLEMENTED_MODELS.get(row.model or "", "gemini"),
                aspect_ratio=row.aspect_ratio,
                reference=reference,
                on_progress=on_progress,
            )

        data: bytes = result["bytes"]
        mime_type: str = result.get("mime_type") or ""
        row.model = result.get("model") or row.model

        if not storage_available():
            raise RuntimeError(
                "No object storage configured. Set STORAGE_BACKEND (r2) and its credentials."
            )

        key = build_key(
            folder=f"generated/{row.project_id}",
            public_id=f"{row.kind}_{row.id}",
            content_type=mime_type,
        )
        upload = get_storage().upload_bytes(data, key=key, content_type=mime_type)

        row.url = upload.url
        row.storage_key = key
        row.mime_type = mime_type
        row.status = "ready"
        row.progress = 100
        row.error_message = None
        db.commit()
        return {"status": "ready", "id": media_id, "url": row.url}

    except GenerationCancelled:
        db.rollback()
        row = db.query(GeneratedMedia).filter(GeneratedMedia.id == media_id).first()
        if row is not None:
            row.status = "cancelled"
            row.progress = 0
            db.commit()
        return {"status": "cancelled", "id": media_id}

    except Exception as exc:  # noqa: BLE001 - the failure must reach the tile
        logger.exception("Generated media %s failed", media_id)
        db.rollback()
        row = db.query(GeneratedMedia).filter(GeneratedMedia.id == media_id).first()
        if row is not None:
            row.status = "failed"
            row.error_message = str(exc)[:2000]
            db.commit()
        raise

    finally:
        db.close()
