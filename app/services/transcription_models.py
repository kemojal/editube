"""Speech-to-text model catalog.

One source of truth for which ASR models the product offers, what each is good
at, and — the part that decides whether a job runs or 500s — **which engine
actually executes it on this box**.

Only the Whisper family has a runtime installed today (`faster-whisper`).
Everything else is listed with its real engine and a `fallback_whisper` size, so
choosing Parakeet gives you a transcript from the closest Whisper build instead
of an error, and swapping in the real runtime later is one adapter plus flipping
`engine_available`.

`accuracy` / `speed` are 0-1 scores used only to draw the meters in the picker.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


#: Engines with a working adapter in this deployment. Everything else runs on
#: its `fallback_whisper` size until an adapter lands.
RUNNABLE_ENGINES: set[str] = {"faster-whisper"}

#: Model used when the user has expressed no preference.
DEFAULT_TRANSCRIPTION_MODEL = "parakeet-v3"

#: Sizes faster-whisper can actually load; guards WHISPER_FALLBACK_SIZE typos.
_VALID_WHISPER_SIZES: set[str] = {
    "tiny", "tiny.en", "base", "base.en", "small", "small.en",
    "medium", "medium.en", "large-v1", "large-v2", "large-v3",
    "large-v3-turbo", "turbo", "distil-large-v3",
}


def _fallback_size_override(model: "TranscriptionModel") -> str | None:
    """Deployment-wide floor for stand-in runs (WHISPER_FALLBACK_SIZE).

    A model's `fallback_whisper` is a per-model guess at "closest Whisper
    build"; boxes with headroom can raise every stand-in at once (e.g.
    `medium`, or `large-v3` on GPU) without touching the catalog or the
    user's picker choice. Explicit faster-whisper selections are untouched —
    the user asked for that exact build. English-only overrides (`*.en`) are
    ignored for multilingual models so the stand-in never loses languages.
    """
    configured = (os.getenv("WHISPER_FALLBACK_SIZE") or "").strip()
    if not configured or configured not in _VALID_WHISPER_SIZES:
        return None
    if configured.endswith(".en") and model.languages != "en":
        return None
    return configured


@dataclass(frozen=True)
class TranscriptionModel:
    id: str
    label: str
    description: str
    engine: str
    #: "multi" | "en" | "ru" — drives the language chip in the picker.
    languages: str
    #: Model can translate non-English speech to English.
    translate: bool
    accuracy: float
    speed: float
    #: Download size in MB. None for models that ship with the runtime.
    size_mb: float | None
    #: faster-whisper size run in place of this model until its engine exists.
    fallback_whisper: str
    badges: list[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.engine in RUNNABLE_ENGINES

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "note": self.description,
            "description": self.description,
            "provider": self.engine,
            "engine": self.engine,
            "languages": self.languages,
            "translate": self.translate,
            "accuracy": self.accuracy,
            "speed": self.speed,
            "size_mb": self.size_mb,
            # Every entry is selectable and produces a transcript; `engine_ready`
            # says whether that happens on its own runtime or on the Whisper
            # stand-in, which is a detail for support, not a reason to disable.
            "available": True,
            "engine_ready": self.available,
            "runs_on": self.engine
            if self.available
            else f"faster-whisper ({_fallback_size_override(self) or self.fallback_whisper})",
            "badges": list(self.badges),
        }


#: Ordered exactly as the picker should present them: the default first, then
#: by how often we expect them to be picked.
TRANSCRIPTION_MODELS: list[TranscriptionModel] = [
    TranscriptionModel(
        id="parakeet-v3",
        label="Parakeet V3",
        description="Fast and accurate.",
        engine="parakeet",
        languages="multi",
        translate=False,
        accuracy=0.86,
        speed=0.84,
        size_mb=None,
        fallback_whisper="small",
        badges=["recommended"],
    ),
    TranscriptionModel(
        id="parakeet-v2",
        label="Parakeet V2",
        description="English only. The best model for English speakers.",
        engine="parakeet",
        languages="en",
        translate=False,
        accuracy=0.88,
        speed=0.86,
        size_mb=451,
        fallback_whisper="small.en",
    ),
    TranscriptionModel(
        id="canary-1b-v2",
        label="Canary 1B v2",
        description="Accurate multilingual. 25 European languages. Supports translation.",
        engine="canary",
        languages="multi",
        translate=True,
        accuracy=0.84,
        speed=0.62,
        size_mb=691,
        fallback_whisper="medium",
    ),
    TranscriptionModel(
        id="whisper-small",
        label="Whisper Small",
        description="Fast and fairly accurate.",
        engine="faster-whisper",
        languages="multi",
        translate=True,
        accuracy=0.64,
        speed=0.78,
        size_mb=465,
        fallback_whisper="small",
    ),
    TranscriptionModel(
        id="whisper-base",
        label="Whisper Base",
        description="Balanced. Comfortable on modest hardware.",
        engine="faster-whisper",
        languages="multi",
        translate=True,
        accuracy=0.55,
        speed=0.90,
        size_mb=142,
        fallback_whisper="base",
    ),
    TranscriptionModel(
        id="moonshine-v2-small",
        label="Moonshine V2 Small",
        description="Fast, English only. Good balance of speed and accuracy.",
        engine="moonshine",
        languages="en",
        translate=False,
        accuracy=0.60,
        speed=0.82,
        size_mb=99,
        fallback_whisper="small.en",
    ),
    TranscriptionModel(
        id="canary-180m-flash",
        label="Canary 180M Flash",
        description="Very fast. English, German, Spanish, French. Supports translation.",
        engine="canary",
        languages="multi",
        translate=True,
        accuracy=0.68,
        speed=0.88,
        size_mb=146,
        fallback_whisper="base",
    ),
    TranscriptionModel(
        id="moonshine-v2-tiny",
        label="Moonshine V2 Tiny",
        description="Ultra-fast, English only.",
        engine="moonshine",
        languages="en",
        translate=False,
        accuracy=0.54,
        speed=0.94,
        size_mb=31,
        fallback_whisper="tiny.en",
    ),
    TranscriptionModel(
        id="whisper-tiny",
        label="Whisper Tiny",
        description="Fastest, least accurate. Good for rough drafts.",
        engine="faster-whisper",
        languages="multi",
        translate=True,
        accuracy=0.42,
        speed=0.96,
        size_mb=75,
        fallback_whisper="tiny",
    ),
    TranscriptionModel(
        id="cohere",
        label="Cohere",
        description="A large, slower, but very accurate multilingual model.",
        engine="cohere",
        languages="multi",
        translate=False,
        accuracy=0.90,
        speed=0.44,
        size_mb=1740,
        fallback_whisper="large-v3",
    ),
    TranscriptionModel(
        id="breeze-asr",
        label="Breeze ASR",
        description="Optimized for Taiwanese Mandarin. Code-switching support.",
        engine="breeze",
        languages="multi",
        translate=False,
        accuracy=0.80,
        speed=0.40,
        size_mb=1024,
        fallback_whisper="medium",
    ),
    TranscriptionModel(
        id="moonshine-base",
        label="Moonshine Base",
        description="Very fast, English only. Handles accents well.",
        engine="moonshine",
        languages="en",
        translate=False,
        accuracy=0.66,
        speed=0.90,
        size_mb=55,
        fallback_whisper="base.en",
    ),
    TranscriptionModel(
        id="sensevoice",
        label="SenseVoice",
        description="Very fast. Chinese, English, Japanese, Korean, Cantonese.",
        engine="sensevoice",
        languages="multi",
        translate=False,
        accuracy=0.78,
        speed=0.92,
        size_mb=152,
        fallback_whisper="small",
    ),
    TranscriptionModel(
        id="whisper-large",
        label="Whisper Large",
        description="Good accuracy, but slow.",
        engine="faster-whisper",
        languages="multi",
        translate=True,
        accuracy=0.88,
        speed=0.30,
        size_mb=1024,
        fallback_whisper="large-v3",
    ),
    TranscriptionModel(
        id="whisper-turbo",
        label="Whisper Turbo",
        description="Balanced accuracy and speed.",
        engine="faster-whisper",
        languages="multi",
        translate=False,
        accuracy=0.86,
        speed=0.56,
        size_mb=1536,
        fallback_whisper="large-v3-turbo",
    ),
    TranscriptionModel(
        id="moonshine-v2-medium",
        label="Moonshine V2 Medium",
        description="English only. High quality.",
        engine="moonshine",
        languages="en",
        translate=False,
        accuracy=0.80,
        speed=0.82,
        size_mb=192,
        fallback_whisper="medium.en",
    ),
    TranscriptionModel(
        id="gigaam-v3",
        label="GigaAM v3",
        description="Russian speech recognition. Fast and accurate.",
        engine="gigaam",
        languages="ru",
        translate=False,
        accuracy=0.84,
        speed=0.80,
        size_mb=151,
        fallback_whisper="small",
    ),
    TranscriptionModel(
        id="whisper-medium",
        label="Whisper Medium",
        description="Good accuracy, medium speed.",
        engine="faster-whisper",
        languages="multi",
        translate=True,
        accuracy=0.82,
        speed=0.60,
        size_mb=469,
        fallback_whisper="medium",
    ),
]

_BY_ID = {model.id: model for model in TRANSCRIPTION_MODELS}

#: The picker used to offer raw faster-whisper sizes ("base", "large-v3"). Rows
#: saved under those ids — and `WHISPER_MODEL_SIZE` — still have to resolve.
#:
#: Every entry must land on a model whose `fallback_whisper` is the *same*
#: Whisper size, or an existing deployment silently changes model on upgrade.
#: That is why `whisper-tiny` / `whisper-base` exist: without them "tiny" and
#: "base" had to round up to the English-only or one-size-larger neighbour.
_LEGACY_WHISPER_IDS = {
    "tiny": "whisper-tiny",
    "tiny.en": "moonshine-v2-tiny",
    "base": "whisper-base",
    "base.en": "moonshine-base",
    "small": "whisper-small",
    "small.en": "moonshine-v2-small",
    "medium": "whisper-medium",
    "medium.en": "moonshine-v2-medium",
    "large": "whisper-large",
    "large-v2": "whisper-large",
    "large-v3": "whisper-large",
    "large-v3-turbo": "whisper-turbo",
    "turbo": "whisper-turbo",
}


def default_transcription_model_id() -> str:
    """Default model id when the user has expressed no preference.

    Both the picker and the worker resolve through here, so the model the
    settings panel shows as the default is always the one that actually runs.
    `WHISPER_MODEL_SIZE` stays in the chain so deployments pinned by env keep
    their model; unset it to get the product default.
    """
    for env_var in ("TRANSCRIPTION_MODEL", "WHISPER_MODEL_SIZE"):
        configured = (os.getenv(env_var) or "").strip()
        if configured:
            resolved = get_transcription_model(configured)
            if resolved:
                return resolved.id
    return DEFAULT_TRANSCRIPTION_MODEL


def get_transcription_model(model_id: str | None) -> TranscriptionModel | None:
    """Resolve a catalog id, tolerating the legacy raw-Whisper-size ids."""
    key = (model_id or "").strip()
    if not key:
        return None
    if key in _BY_ID:
        return _BY_ID[key]
    legacy = _LEGACY_WHISPER_IDS.get(key)
    return _BY_ID.get(legacy) if legacy else None


def resolve_runtime(model_id: str | None) -> tuple[TranscriptionModel, str, bool]:
    """Pick the model to record and the Whisper size to actually run.

    Returns ``(model, whisper_size, is_fallback)``. ``is_fallback`` is True when
    the chosen model's engine has no adapter here and Whisper stands in for it —
    the caller logs that so a "why does Parakeet sound like Whisper" question has
    an answer in the worker output. Stand-in runs honour WHISPER_FALLBACK_SIZE
    (see `_fallback_size_override`); explicit Whisper picks run exactly what the
    user chose.
    """
    model = get_transcription_model(model_id) or get_transcription_model(
        default_transcription_model_id()
    )
    assert model is not None  # the default id is always in the catalog
    size = model.fallback_whisper
    if not model.available:
        size = _fallback_size_override(model) or size
    return model, size, not model.available


def transcription_catalog_payload() -> list[dict]:
    return [model.to_dict() for model in TRANSCRIPTION_MODELS]
