"""AI media generation — images (Gemini) and video (Veo).

Ported from CutScript's `src-tauri/src/ai/media.rs`. The shape is the same
(prompt in, bytes out, progress along the way); what changes is where it runs.
CutScript called Gemini from Rust on the user's machine with a keychain key;
here it runs in an RQ worker with the server's key, so no key ever reaches the
browser.

Video generation is a long-running operation: Veo returns an operation handle
and we poll it. The poll loop reports progress and honours a cancel flag so a
user can abandon a job that is going to take minutes.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-image-preview")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_VIDEO_MODEL = os.getenv("GEMINI_VIDEO_MODEL", "veo-3.1-generate-preview")

#: Veo jobs routinely run for minutes; give up rather than hold a worker forever.
VIDEO_POLL_INTERVAL_SEC = float(os.getenv("AI_VIDEO_POLL_INTERVAL_SEC", "5"))
VIDEO_TIMEOUT_SEC = float(os.getenv("AI_VIDEO_TIMEOUT_SEC", "900"))

ProgressFn = Callable[[int, str], None]
CancelFn = Callable[[], bool]


class GenerationCancelled(RuntimeError):
    """Raised when the user cancelled the job while it was running."""


def provider_availability() -> dict[str, bool]:
    """Which providers this deployment can actually call.

    The client asks for this instead of hardcoding a list, so adding a key to
    the server is enough to light a provider up in the picker.
    """
    return {
        "gemini": bool(os.getenv("GEMINI_API_KEY", "").strip()),
        "openrouter": bool(os.getenv("OPENROUTER_API_KEY", "").strip()),
        "seedance": False,
        "kling": False,
        "runway": False,
    }


def _openrouter_image(
    *,
    prompt: str,
    model: str,
    reference: dict[str, str] | None = None,
    on_progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Image generation through OpenRouter's chat-completions surface.

    OpenRouter returns generated images on the assistant message as data URLs
    rather than as a separate media field, so the response is walked for the
    first `images[].image_url.url` and decoded.
    """
    import requests

    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    if on_progress:
        on_progress(10, "Contacting OpenRouter")

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    if reference:
        data = reference.get("data_base64") or reference.get("dataBase64")
        mime = reference.get("mime_type") or reference.get("mimeType") or "image/png"
        if data:
            content.append(
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}
            )

    response = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # OpenRouter attributes traffic with these; harmless if unset.
            "HTTP-Referer": os.getenv("FRONTEND_BASE_URL", "https://editube.app"),
            "X-Title": "editube",
        },
        json={
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "modalities": ["image", "text"],
        },
        timeout=180,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"OpenRouter error {response.status_code}: {response.text[:400]}")

    payload = response.json()
    if on_progress:
        on_progress(80, "Decoding image")

    for choice in payload.get("choices") or []:
        message = choice.get("message") or {}
        for image in message.get("images") or []:
            url = ((image or {}).get("image_url") or {}).get("url") or ""
            if url.startswith("data:"):
                header, _, encoded = url.partition(",")
                mime_type = header.split(";")[0].removeprefix("data:") or "image/png"
                return {"bytes": base64.b64decode(encoded), "mime_type": mime_type, "model": model}
    raise RuntimeError("OpenRouter returned no image data")


def _client():
    from google import genai

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    return genai.Client(api_key=api_key)


def _reference_part(reference: dict[str, str] | None) -> Any | None:
    """Turn a caller-supplied reference image into an inline data part."""
    if not reference:
        return None
    data = reference.get("data_base64") or reference.get("dataBase64")
    mime = reference.get("mime_type") or reference.get("mimeType") or "image/png"
    if not data:
        return None
    from google.genai import types

    return types.Part.from_bytes(data=base64.b64decode(data), mime_type=mime)


def generate_image(
    *,
    prompt: str,
    model: str | None = None,
    provider: str = "gemini",
    aspect_ratio: str | None = None,
    reference: dict[str, str] | None = None,
    on_progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Generate a single image. Returns ``{bytes, mime_type, model}``."""
    model_id = model or DEFAULT_IMAGE_MODEL
    if provider == "openrouter":
        return _openrouter_image(
            prompt=prompt, model=model_id, reference=reference, on_progress=on_progress
        )

    if on_progress:
        on_progress(10, "Contacting the image model")

    client = _client()

    contents: list[Any] = [prompt]
    part = _reference_part(reference)
    if part is not None:
        contents.append(part)

    config: dict[str, Any] = {"response_modalities": ["IMAGE"]}
    if aspect_ratio:
        config["image_config"] = {"aspect_ratio": aspect_ratio}

    response = client.models.generate_content(model=model_id, contents=contents, config=config)
    if on_progress:
        on_progress(80, "Decoding image")

    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for item in getattr(content, "parts", None) or []:
            inline = getattr(item, "inline_data", None)
            if inline and getattr(inline, "data", None):
                return {
                    "bytes": inline.data,
                    "mime_type": getattr(inline, "mime_type", None) or "image/png",
                    "model": model_id,
                }
    raise RuntimeError("The image model returned no image data")


def generate_video(
    *,
    prompt: str,
    model: str | None = None,
    aspect_ratio: str | None = None,
    duration_seconds: float | None = None,
    reference: dict[str, str] | None = None,
    on_progress: ProgressFn | None = None,
    is_cancelled: CancelFn | None = None,
) -> dict[str, Any]:
    """Generate a video, polling the long-running operation to completion.

    Returns ``{bytes, mime_type, model}``. Raises :class:`GenerationCancelled`
    if the cancel flag flips while polling.
    """
    client = _client()
    model_id = model or DEFAULT_VIDEO_MODEL

    if on_progress:
        on_progress(5, "Submitting video job")

    kwargs: dict[str, Any] = {"model": model_id, "prompt": prompt}
    config: dict[str, Any] = {}
    if aspect_ratio:
        config["aspect_ratio"] = aspect_ratio
    if duration_seconds:
        config["duration_seconds"] = int(duration_seconds)
    if config:
        kwargs["config"] = config
    part = _reference_part(reference)
    if part is not None:
        kwargs["image"] = part

    operation = client.models.generate_videos(**kwargs)

    started = time.monotonic()
    while not getattr(operation, "done", False):
        if is_cancelled and is_cancelled():
            raise GenerationCancelled("Cancelled while generating")
        elapsed = time.monotonic() - started
        if elapsed > VIDEO_TIMEOUT_SEC:
            raise RuntimeError(
                f"Video generation timed out after {int(elapsed)}s"
            )
        if on_progress:
            # No real percentage is available from the operation, so ramp
            # towards 90% over the expected duration: honest about being an
            # estimate, still useful as a sign of life.
            ramp = min(90, 10 + int(80 * (elapsed / max(VIDEO_TIMEOUT_SEC * 0.4, 1))))
            on_progress(ramp, "Generating video")
        time.sleep(VIDEO_POLL_INTERVAL_SEC)
        operation = client.operations.get(operation)

    if on_progress:
        on_progress(92, "Downloading video")

    data = _extract_video_bytes(client, operation)
    if not data:
        raise RuntimeError("The video model returned no video data")
    return {"bytes": data, "mime_type": "video/mp4", "model": model_id}


def _extract_video_bytes(client: Any, operation: Any) -> bytes | None:
    """Pull bytes out of a finished operation.

    The response shape varies between models and SDK versions (inline bytes vs
    a file handle vs a URI), so every known shape is tried before giving up —
    CutScript hit the same variance and its spec calls for defensive parsing.
    """
    response = getattr(operation, "response", None) or getattr(operation, "result", None)
    samples = (
        getattr(response, "generated_videos", None)
        or getattr(response, "generatedSamples", None)
        or []
    )
    for sample in samples:
        video = getattr(sample, "video", None) or sample
        inline = getattr(video, "video_bytes", None) or getattr(video, "inline_data", None)
        if isinstance(inline, (bytes, bytearray)):
            return bytes(inline)
        data = getattr(inline, "data", None)
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)
        # File handle: ask the SDK to download it.
        try:
            if getattr(video, "uri", None) or getattr(video, "name", None):
                downloaded = client.files.download(file=video)
                if isinstance(downloaded, (bytes, bytearray)):
                    return bytes(downloaded)
                blob = getattr(downloaded, "video_bytes", None)
                if isinstance(blob, (bytes, bytearray)):
                    return bytes(blob)
        except Exception:  # noqa: BLE001 - fall through to the next sample
            logger.exception("Failed to download generated video sample")
    return None
