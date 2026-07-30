from __future__ import annotations

import json
import os
from typing import Any


GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")


def _get_client():
    from google import genai

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    return genai.Client(api_key=api_key)


def generate_text(prompt: str, system: str | None = None) -> str:
    client = _get_client()
    if system:
        prompt = f"{system}\n\n{prompt}"
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
    )
    text = getattr(response, "text", None)
    if text is None:
        raise RuntimeError("Gemini returned an empty response")
    return text.strip()


_JSON_ONLY = "Return valid JSON only. Do not wrap in markdown fences.\n"


def _parse_json(raw: str, fallback: dict[str, Any]) -> dict[str, Any]:
    """Parse a model response as JSON, tolerating a ```json fence it was told
    not to emit but sometimes does anyway."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[: -len("```")]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return fallback
    return parsed if isinstance(parsed, dict) else fallback


def generate_json(prompt: str, fallback: dict[str, Any]) -> dict[str, Any]:
    return _parse_json(generate_text(prompt=f"{_JSON_ONLY}{prompt}"), fallback)


def generate_json_multimodal(
    prompt: str,
    images: list[bytes],
    fallback: dict[str, Any],
    *,
    mime_type: str = "image/jpeg",
) -> dict[str, Any]:
    """``generate_json`` with image parts attached — the model sees the frames
    alongside the prompt. Images are sent in order, so the prompt can refer to
    them positionally ("frame 1 is at 0.6s"). Falls back to a text-only call if
    the installed SDK can't build image parts."""
    if not images:
        return generate_json(prompt, fallback)

    client = _get_client()
    full_prompt = f"{_JSON_ONLY}{prompt}"
    try:
        from google.genai import types

        contents: list[Any] = [
            types.Part.from_bytes(data=blob, mime_type=mime_type) for blob in images
        ]
        contents.append(full_prompt)
    except (ImportError, AttributeError):
        return generate_json(prompt, fallback)

    response = client.models.generate_content(model=GEMINI_MODEL, contents=contents)
    text = getattr(response, "text", None)
    if not text:
        return fallback
    return _parse_json(text, fallback)


def generate_broll_image(transcript_text: str) -> dict[str, Any]:
    """Two-step pipeline: analyse transcript → generate cinematic B-roll image."""
    client = _get_client()

    # Step A — derive an image prompt from the transcript excerpt
    meta = generate_json(
        f"Given this transcript excerpt, create a cinematic B-roll image prompt.\n"
        f'Return JSON: {{"prompt": "...", "keyword": "one-word subject"}}\n\n'
        f"Transcript: {transcript_text}",
        fallback={"prompt": transcript_text, "keyword": "scene"},
    )
    image_prompt = str(meta.get("prompt", transcript_text))
    keyword = str(meta.get("keyword", "scene"))

    # Step B — generate the image with the image-capable model
    from google.genai import types

    response = client.models.generate_content(
        model="gemini-3.1-flash-image-preview",
        contents=f"Generate a cinematic B-roll photograph: {image_prompt}",
        config=types.GenerateContentConfig(response_modalities=["Text", "Image"]),
    )
    for part in response.candidates[0].content.parts:
        if part.inline_data:
            return {
                "prompt": image_prompt,
                "keyword": keyword,
                "image_bytes": part.inline_data.data,
                "mime_type": part.inline_data.mime_type or "image/png",
            }
    raise RuntimeError("Gemini returned no image data")
