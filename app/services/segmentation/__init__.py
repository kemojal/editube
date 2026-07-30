"""Segmentation providers.

`SEGMENTATION_PROVIDER` selects the backend: `local` (default), `http`, or
`auto`, which prefers a configured remote service and falls back to local.
"""

from __future__ import annotations

import os

from .base import SegmentationError, SegmentationProvider
from .http import HttpSegmentationProvider
from .local import LocalSegmentationProvider

__all__ = [
    "SegmentationError",
    "SegmentationProvider",
    "get_provider",
]


def get_provider() -> SegmentationProvider:
    choice = os.environ.get("SEGMENTATION_PROVIDER", "auto").strip().lower()

    if choice == "http":
        return HttpSegmentationProvider()
    if choice == "local":
        return LocalSegmentationProvider()

    # auto: a configured remote service wins, because it is the faster path when
    # someone has gone to the trouble of standing one up.
    remote = HttpSegmentationProvider()
    if remote.is_available()[0]:
        return remote
    return LocalSegmentationProvider()
