"""Sync a provider's avatar/voice catalog into the DB.

The picker reads ``aiugc.ugc_avatars`` / ``ugc_voices`` (curation + premium
flags persist there). This pulls a provider's live catalog and upserts it so
real providers (HeyGen) show their creators instead of the seeded stub set.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.db.models import UgcAvatar, UgcVoice
from app.ugc_providers import get_avatar_provider

logger = logging.getLogger(__name__)


def sync_provider_catalog(db: Session, provider_name: str) -> dict[str, int]:
    """Upsert avatars + voices for ``provider_name`` from its live API.

    Returns counts of avatars/voices synced.
    """
    provider = get_avatar_provider(provider_name)
    pname = provider.name

    avatars = provider.list_avatars()
    voices = provider.list_voices()

    a_count = 0
    for spec in avatars:
        row = (
            db.query(UgcAvatar)
            .filter(UgcAvatar.provider == pname, UgcAvatar.provider_avatar_id == spec.provider_avatar_id)
            .first()
        )
        if row is None:
            row = UgcAvatar(provider=pname, provider_avatar_id=spec.provider_avatar_id)
            db.add(row)
        row.name = spec.name
        row.thumbnail_url = spec.thumbnail_url
        row.age_range = spec.age_range
        row.gender_presentation = spec.gender_presentation
        row.region = spec.region
        row.default_voice_id = spec.default_voice_id
        row.accent = spec.accent
        row.energy = spec.energy
        row.is_active = True
        row.is_premium = spec.is_premium
        a_count += 1

    v_count = 0
    for spec in voices:
        row = (
            db.query(UgcVoice)
            .filter(UgcVoice.provider == pname, UgcVoice.provider_voice_id == spec.provider_voice_id)
            .first()
        )
        if row is None:
            row = UgcVoice(provider=pname, provider_voice_id=spec.provider_voice_id)
            db.add(row)
        row.name = spec.name
        row.gender = spec.gender
        row.accent = spec.accent
        row.language = spec.language
        row.preview_url = spec.preview_url
        row.is_premium = spec.is_premium
        v_count += 1

    db.commit()
    logger.info("synced %s catalog: %d avatars, %d voices", pname, a_count, v_count)
    return {"provider": pname, "avatars": a_count, "voices": v_count}
