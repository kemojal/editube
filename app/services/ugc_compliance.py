"""AI-content disclosure + platform guidance.

Disclosure is ON by default and configurable per workspace via
``workspace.settings['ugc_disclosure'] = false``. The renderer burns a small
"AI-generated" label; metadata flags live on the ``UgcVariation`` row
(``is_ai_generated`` / ``disclosure_applied``).
"""

from __future__ import annotations

from typing import Any

DISCLOSURE_LABEL = "AI-generated"

_PLATFORM_GUIDANCE = {
    "meta": "Meta requires advertisers to disclose AI-generated/altered content for social issues, "
    "elections and politics, and labels realistic AI content. Keep the on-video AI label and complete "
    "Meta's AI-content disclosure at upload.",
    "tiktok": "TikTok requires creators to label realistic AI-generated content; use the AIGC toggle and "
    "keep the on-video label.",
    "reels": "Instagram/Reels labels AI content and may auto-detect it; keep the disclosure and use the "
    "'AI info' label when publishing.",
    "shorts": "YouTube requires disclosing realistic altered/synthetic content; set the 'altered content' "
    "flag on the Short.",
}


def disclosure_enabled(workspace_settings: dict[str, Any] | None) -> bool:
    if not isinstance(workspace_settings, dict):
        return True
    val = workspace_settings.get("ugc_disclosure")
    if val is None:
        return True
    return bool(val)


def platform_guidance(platform: str | None) -> str:
    return _PLATFORM_GUIDANCE.get((platform or "").lower(), _PLATFORM_GUIDANCE["meta"])
