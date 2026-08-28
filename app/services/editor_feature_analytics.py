"""Privacy-safe feature boundaries derived from rough-cut draft changes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def _items(payload: dict[str, Any], key: str) -> list[Any]:
    value = payload.get(key)
    return value if isinstance(value, list) else []


def _attributes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    value = payload.get("clipAttributes")
    if not isinstance(value, dict):
        return []
    return [item for item in value.values() if isinstance(item, dict)]


def _any_attribute(payload: dict[str, Any], predicate: Callable[[dict[str, Any]], bool]) -> bool:
    return any(predicate(item) for item in _attributes(payload))


def _enabled_dict(value: Any) -> bool:
    return isinstance(value, dict) and value.get("enabled") is not False and bool(value)


def _active_features(payload: dict[str, Any]) -> set[str]:
    text_overlay = payload.get("textOverlay")
    active: set[str] = set()
    if (
        (isinstance(text_overlay, dict) and bool(text_overlay.get("enabled")))
        or _items(payload, "textOverlays")
        or _items(payload, "lowerThirds")
    ):
        active.add("text_overlay")
    if _items(payload, "gridClips"):
        active.add("grid")
    if _items(payload, "transitions"):
        active.add("transitions")
    if payload.get("showFillers") is True:
        active.add("filler_removal")
    if payload.get("removeSilence") is True:
        active.add("silence_removal")
    if payload.get("smoothSpeech") is True:
        active.add("bad_take_removal")
    if _any_attribute(payload, lambda item: bool(item.get("masks")) or _enabled_dict(item.get("mask"))):
        active.add("masking")
    if _any_attribute(payload, lambda item: bool(item.get("keyframes"))):
        active.add("keyframes")
    if _any_attribute(payload, lambda item: _enabled_dict(item.get("adjust"))):
        active.add("color_adjust")
    if _any_attribute(payload, lambda item: _enabled_dict(item.get("audio"))):
        active.add("audio_edit")
    if _any_attribute(payload, lambda item: _enabled_dict(item.get("animation"))):
        active.add("animation")
    if _any_attribute(payload, lambda item: _enabled_dict(item.get("retouch"))):
        active.add("retouch")
    if _any_attribute(
        payload,
        lambda item: isinstance(item.get("removeBg"), dict)
        and bool(item["removeBg"].get("chromaKey")),
    ):
        active.add("chroma_key")
    if _any_attribute(
        payload,
        lambda item: isinstance(item.get("removeBg"), dict)
        and bool(
            item["removeBg"].get("autoRemoval")
            or item["removeBg"].get("customRemoval")
        ),
    ):
        active.add("background_removal")
    return active


def changed_active_editor_features(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
) -> tuple[str, ...]:
    """Features newly applied or changed in a successfully persisted draft.

    Payload content is only compared in-process. The caller emits stable IDs
    and feature keys; user-authored text and style payloads never leave the
    first-party database through this helper.
    """

    prior = previous if isinstance(previous, dict) else {}
    active = _active_features(current)
    changed: list[str] = []
    fields = {
        "text_overlay": ("textOverlay", "textOverlays", "lowerThirds"),
        "grid": ("gridClips",),
        "transitions": ("transitions",),
        "filler_removal": ("showFillers",),
        "silence_removal": ("removeSilence",),
        "bad_take_removal": ("smoothSpeech",),
        "masking": ("clipAttributes",),
        "keyframes": ("clipAttributes",),
        "color_adjust": ("clipAttributes",),
        "audio_edit": ("clipAttributes",),
        "animation": ("clipAttributes",),
        "retouch": ("clipAttributes",),
        "chroma_key": ("clipAttributes",),
        "background_removal": ("clipAttributes",),
    }
    for feature_key in sorted(active):
        if any(prior.get(key) != current.get(key) for key in fields[feature_key]):
            changed.append(feature_key)
    return tuple(changed)
