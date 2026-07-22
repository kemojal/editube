"""AI brief generation: product → marketing fundamentals for UGC ads."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _product_summary(product: dict[str, Any]) -> str:
    parts = [
        f"Name: {product.get('name') or ''}",
        f"Brand: {product.get('brand') or ''}",
        f"Price: {product.get('price') or ''} {product.get('currency') or ''}".strip(),
        f"Description: {product.get('description') or ''}",
    ]
    if product.get("benefits"):
        parts.append("Benefits: " + "; ".join(map(str, product["benefits"][:10])))
    if product.get("pain_points"):
        parts.append("Pain points: " + "; ".join(map(str, product["pain_points"][:10])))
    if product.get("reviews"):
        parts.append("Reviews: " + " | ".join(map(str, product["reviews"][:5])))
    return "\n".join(p for p in parts if p.strip())[:6000]


def generate_brief(product: dict[str, Any]) -> dict[str, Any]:
    """Return a brief dict: audience, main_promise, pain_points, objections, benefits, angles."""
    from app.services.ai_client import generate_json

    summary = _product_summary(product)
    fallback = {
        "audience": (product.get("target_audience") or {}).get("who") if isinstance(product.get("target_audience"), dict) else None,
        "main_promise": None,
        "pain_points": product.get("pain_points") or [],
        "objections": [],
        "benefits": product.get("benefits") or [],
        "angles": [],
    }
    if not summary.strip():
        return fallback
    try:
        result = generate_json(
            "You are a senior direct-response strategist. From this product, produce a tight creative brief "
            "for short-form UGC video ads (TikTok/Reels/Shorts).\n"
            'Return JSON: {"audience":"one sentence ICP","main_promise":"the core promise",'
            '"pain_points":["..."],"objections":["..."],"benefits":["..."],'
            '"angles":["problem-solution","testimonial", "..."]}\n\n'
            f"PRODUCT:\n{summary}",
            fallback=fallback,
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("brief generation falling back (AI unavailable): %s", exc)
        return fallback

    # Normalize shapes.
    for k in ("pain_points", "objections", "benefits", "angles"):
        v = result.get(k)
        result[k] = [str(x) for x in v][:12] if isinstance(v, list) else fallback.get(k, [])
    result["audience"] = result.get("audience") or fallback["audience"]
    result["main_promise"] = result.get("main_promise") or fallback["main_promise"]
    return result
