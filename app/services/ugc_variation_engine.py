"""One product → N UGC ad variations.

Samples a capped, diversity-weighted, de-duplicated set of variations across
{script/angle × hook × avatar × voice × CTA × length × aspect × caption style},
reserves credits up front, persists ``UgcVariation`` rows, and enqueues one
render per row. Mirrors the fan-out in ``repurpose_pipeline``.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import UgcAvatar, UgcBrief, UgcCampaign, UgcVariation
from app.services import ugc_credits
from app.ugc_providers import get_avatar_provider

logger = logging.getLogger(__name__)


class InsufficientCreditsError(Exception):
    def __init__(self, needed: int, available: int) -> None:
        self.needed = needed
        self.available = available
        super().__init__(f"needs {needed} credits, has {available}")


def _max_batch() -> int:
    try:
        return max(1, int(os.environ.get("UGC_MAX_VARIATIONS_PER_BATCH", "100")))
    except ValueError:
        return 100


def _catalog_avatars(db: Session, provider: str) -> list[dict[str, Any]]:
    """Avatars for fan-out: persisted catalog first, then live provider, then a stub."""
    rows = (
        db.query(UgcAvatar)
        .filter(UgcAvatar.is_active.is_(True), UgcAvatar.provider == provider)
        .all()
    )
    if rows:
        return [
            {"provider_avatar_id": a.provider_avatar_id, "name": a.name, "default_voice_id": a.default_voice_id}
            for a in rows
        ]
    try:
        specs = get_avatar_provider(provider).list_avatars()
        if specs:
            return [
                {"provider_avatar_id": s.provider_avatar_id, "name": s.name, "default_voice_id": s.default_voice_id}
                for s in specs
            ]
    except Exception:  # noqa: BLE001
        logger.exception("live avatar fetch failed for provider %s", provider)
    return [{"provider_avatar_id": "ugc_f_us_1", "name": "Creator", "default_voice_id": None}]


def _scripts_from_brief(brief: UgcBrief | None, hooks: list[str]) -> list[dict[str, str]]:
    scripts = list(brief.scripts or []) if brief else []
    scripts = [s for s in scripts if isinstance(s, dict) and s.get("script")]
    if scripts:
        return scripts
    # Fall back to building skeleton scripts from hooks/angles.
    angles = list(brief.angles or []) if brief else []
    angles = angles or ["problem-solution", "testimonial", "founder-story"]
    base = hooks or ["Here's why this is different."]
    return [
        {"angle": angles[i % len(angles)], "hook": base[i % len(base)], "script": base[i % len(base)]}
        for i in range(max(len(angles), len(base)))
    ]


def sample_combos(
    *,
    scripts: list[dict[str, Any]],
    hooks: list[str],
    ctas: list[Any],
    avatars: list[dict[str, Any]],
    voices: list[Any],
    lengths: list[int],
    aspects: list[str],
    caption_styles: list[Any],
    count: int,
) -> list[dict[str, Any]]:
    """Pure, DB-free diversity-weighted, de-duplicated sampling.

    Different divisors per dimension rotate the option pools out of phase so a
    small request still spans angles/avatars/hooks rather than repeating.
    """
    scripts = scripts or [{"angle": "problem-solution", "hook": "", "script": ""}]
    ctas = ctas or [None]
    avatars = avatars or [{"provider_avatar_id": "ugc_f_us_1", "name": "Creator"}]
    lengths = lengths or [30]
    aspects = aspects or ["9:16"]
    caption_styles = caption_styles or [None]

    seen: set[str] = set()
    combos: list[dict[str, Any]] = []
    i = 0
    while len(combos) < count and i < count * 25:
        script = scripts[i % len(scripts)]
        avatar = avatars[i % len(avatars)]
        voice_id = (voices[(i // 2) % len(voices)] if voices else None) or avatar.get("default_voice_id")
        cta = ctas[(i // 2) % len(ctas)]
        length = lengths[(i // 3) % len(lengths)]
        aspect = aspects[(i // 5) % len(aspects)]
        caption = caption_styles[(i // 7) % len(caption_styles)]
        hook = script.get("hook") or (hooks[i % len(hooks)] if hooks else "")
        i += 1
        key = json.dumps(
            [hook, script.get("script"), avatar.get("provider_avatar_id"), voice_id, cta, length, aspect, caption],
            sort_keys=True,
            default=str,
        )
        if key in seen:
            continue
        seen.add(key)
        combos.append(
            {
                "angle": script.get("angle"),
                "hook": hook,
                "script": script.get("script"),
                "cta": cta,
                "avatar": avatar,
                "voice_id": voice_id,
                "length": length,
                "aspect": aspect,
                "caption": caption,
            }
        )
    return combos


def build_variations(
    db: Session,
    campaign: UgcCampaign,
    count: int,
    dimensions: dict[str, Any] | None = None,
) -> list[int]:
    dimensions = dimensions or {}
    count = max(1, min(int(count), _max_batch()))

    brief = (
        db.query(UgcBrief).filter(UgcBrief.id == campaign.brief_id).first()
        if campaign.brief_id
        else None
    )

    hooks = [str(h) for h in (dimensions.get("hooks") or (brief.hooks if brief else []) or [])]
    ctas = [str(c) for c in (dimensions.get("ctas") or (brief.ctas if brief else []) or [])] or [None]
    scripts = _scripts_from_brief(brief, hooks)
    provider = os.environ.get("UGC_AVATAR_PROVIDER", "stub").strip().lower() or "stub"
    avatars = dimensions.get("avatars") or _catalog_avatars(db, provider)
    voices = dimensions.get("voices") or []
    from app.services.ugc_platforms import max_length_for

    _max_len = max_length_for(campaign.platform)
    lengths = [
        min(int(x), _max_len)
        for x in (dimensions.get("lengths") or [campaign.default_length_sec or 30])
    ]
    aspects = dimensions.get("aspect_ratios") or [campaign.default_aspect_ratio or "9:16"]
    caption_styles = dimensions.get("caption_styles") or [None]

    # --- reserve credits before creating anything ---
    cost = count * ugc_credits.credit_cost_per_variation()
    if not ugc_credits.reserve(db, campaign.workspace_id, cost):
        raise InsufficientCreditsError(cost, ugc_credits.balance(db, campaign.workspace_id))

    combos = sample_combos(
        scripts=scripts,
        hooks=hooks,
        ctas=ctas,
        avatars=avatars,
        voices=voices,
        lengths=lengths,
        aspects=aspects,
        caption_styles=caption_styles,
        count=count,
    )

    created_ids: list[int] = []
    for idx, c in enumerate(combos, start=1):
        var = UgcVariation(
            campaign_id=campaign.id,
            name=f"{(c['angle'] or 'ad').replace('-', ' ').title()} · {c['avatar'].get('name', 'Creator')} #{idx}",
            angle=c["angle"],
            hook=c["hook"],
            script=c["script"],
            cta=c["cta"],
            caption_style=c["caption"],
            provider=provider,
            provider_avatar_id=c["avatar"].get("provider_avatar_id"),
            provider_voice_id=c["voice_id"],
            avatar_name=c["avatar"].get("name"),
            aspect_ratio=c["aspect"],
            length_sec=c["length"],
            status="queued",
        )
        db.add(var)
        db.flush()
        created_ids.append(var.id)

    campaign.status = "generating"
    db.commit()

    # Enqueue a render per variation (fire-and-forget, like repurpose auto-render).
    from app.jobs.queue import enqueue_ugc_render_job

    for vid in created_ids:
        rq_job_id = enqueue_ugc_render_job(vid)
        if rq_job_id:
            row = db.query(UgcVariation).filter(UgcVariation.id == vid).first()
            if row:
                row.rq_job_id = rq_job_id
    db.commit()

    # Refund variations we reserved but couldn't build (e.g. tiny pools).
    shortfall = count - len(created_ids)
    if shortfall > 0:
        ugc_credits.refund(db, campaign.workspace_id, shortfall * ugc_credits.credit_cost_per_variation())

    return created_ids
