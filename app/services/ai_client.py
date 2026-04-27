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


def generate_json(prompt: str, fallback: dict[str, Any]) -> dict[str, Any]:
    raw = generate_text(
        prompt=(
            "Return valid JSON only. Do not wrap in markdown fences.\n"
            f"{prompt}"
        )
    )
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return fallback


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
