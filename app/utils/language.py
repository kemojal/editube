"""Spoken-language normalization shared by transcription request plumbing.

"auto" (the wizard default), an empty string, and None all mean auto-detect
and normalize to None so callers/storage never need to special-case them.
"""

from __future__ import annotations

from typing import Optional


def normalize_language(value: Optional[str]) -> Optional[str]:
    """Return a lowercase ISO 639-1 code, or None for auto-detect."""
    if value is None:
        return None
    v = value.strip().lower()
    if not v or v == "auto":
        return None
    return v
