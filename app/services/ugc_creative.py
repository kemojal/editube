"""Creative generation: hooks, scripts, and CTAs for UGC ads.

Built on the same Gemini ``generate_json`` helper. Each generator degrades to a
deterministic template fallback so the pipeline still produces usable creative
when the model is unavailable (and so unit tests can run without a key).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Seed angle library (AIUGC.md §8). The engine samples across these.
ANGLES: list[str] = [
    "problem-solution",
    "testimonial",
    "i-wish-i-found-this-earlier",
    "founder-story",
    "routine-demo",
    "street-interview",
    "app-demo",
    "before-after",
    "myth-busting",
    "comparison",
    "tiktok-made-me-buy-it",
    "gift-recommendation",
    "pain-point-rant",
    "soft-sell-lifestyle",
    "direct-response-offer",
]

_DEFAULT_CTAS = [
    "Tap the link to try it today.",
    "Get yours before it sells out.",
    "Check the link in bio.",
]


def _brief_summary(brief: dict[str, Any], product: dict[str, Any]) -> str:
    parts = [
        f"Product: {product.get('name') or ''}",
        f"Audience: {brief.get('audience') or ''}",
        f"Promise: {brief.get('main_promise') or ''}",
    ]
    if brief.get("benefits"):
        parts.append("Benefits: " + "; ".join(map(str, brief["benefits"][:8])))
    if brief.get("pain_points"):
        parts.append("Pain points: " + "; ".join(map(str, brief["pain_points"][:8])))
    return "\n".join(p for p in parts if p.strip())[:4000]


def generate_hooks(brief: dict[str, Any], product: dict[str, Any], count: int = 20) -> list[str]:
    from app.services.ai_client import generate_json

    name = product.get("name") or "this"
    fallback = [
        f"I didn't expect {name} to actually work…",
        f"Stop scrolling if you struggle with {(brief.get('pain_points') or ['this'])[0]}.",
        f"This is the {name} I wish I found earlier.",
        "Nobody talks about this problem…",
        f"I tried {name} for 7 days. Here's what happened.",
    ]
    try:
        result = generate_json(
            f"Write {count} scroll-stopping first-3-second hooks for UGC video ads. "
            "Native, casual, spoken — not slogans. Vary the angle.\n"
            'Return JSON: {"hooks":["..."]}\n\n'
            f"{_brief_summary(brief, product)}",
            fallback={"hooks": fallback},
        )
        hooks = [str(h).strip() for h in (result.get("hooks") or []) if str(h).strip()]
        return hooks[:count] or fallback
    except Exception as exc:  # noqa: BLE001
        logger.info("hook generation fallback: %s", exc)
        return fallback


def generate_scripts(
    brief: dict[str, Any], product: dict[str, Any], angles: list[str] | None = None, count: int = 10
) -> list[dict[str, str]]:
    """Return [{angle, hook, script}] mini-scripts, one per requested angle."""
    from app.services.ai_client import generate_json

    use_angles = (angles or brief.get("angles") or ANGLES)[:count] or ANGLES[:count]
    name = product.get("name") or "the product"
    fallback = [
        {
            "angle": a,
            "hook": f"Here's why {name} is different.",
            "script": (
                f"Hook: Here's why {name} is different.\n"
                f"Body: {brief.get('main_promise') or 'It just works.'} "
                f"{'; '.join(map(str, (brief.get('benefits') or [])[:2]))}.\n"
                "CTA: Tap the link to try it."
            ),
        }
        for a in use_angles
    ]
    try:
        result = generate_json(
            f"Write {len(use_angles)} short UGC ad scripts (~60-90 words each, spoken first person), "
            f"one for each of these angles: {', '.join(use_angles)}. "
            "Each script: a hook, a body that earns trust, and a soft CTA.\n"
            'Return JSON: {"scripts":[{"angle":"..","hook":"..","script":".."}]}\n\n'
            f"{_brief_summary(brief, product)}",
            fallback={"scripts": fallback},
        )
        scripts = []
        for s in result.get("scripts") or []:
            if isinstance(s, dict) and s.get("script"):
                scripts.append(
                    {
                        "angle": str(s.get("angle") or "problem-solution"),
                        "hook": str(s.get("hook") or ""),
                        "script": str(s.get("script")),
                    }
                )
        return scripts or fallback
    except Exception as exc:  # noqa: BLE001
        logger.info("script generation fallback: %s", exc)
        return fallback


def generate_ctas(brief: dict[str, Any], product: dict[str, Any], count: int = 3) -> list[str]:
    from app.services.ai_client import generate_json

    try:
        result = generate_json(
            f"Write {count} short, native CTAs for a UGC ad (≤8 words each).\n"
            'Return JSON: {"ctas":["..."]}\n\n'
            f"{_brief_summary(brief, product)}",
            fallback={"ctas": _DEFAULT_CTAS},
        )
        ctas = [str(c).strip() for c in (result.get("ctas") or []) if str(c).strip()]
        return ctas[:count] or _DEFAULT_CTAS
    except Exception as exc:  # noqa: BLE001
        logger.info("cta generation fallback: %s", exc)
        return _DEFAULT_CTAS
