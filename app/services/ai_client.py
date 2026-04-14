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
