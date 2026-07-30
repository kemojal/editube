"""Segmentation provider contract.

Runtime is a deployment choice, not a code choice. Feature code never imports a
model — it calls `get_provider()` and gets whichever backend this deployment is
configured for, so moving from in-process to a GPU service or a hosted API is an
environment change rather than a rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


#: Capability names, referenced by both the provider and the API that reports
#: them to the editor. Auto matting needs a salient-object model; point prompts
#: need a promptable one; propagation needs video memory across frames.
CAPABILITY_AUTO_MATTE = "auto_matte"
CAPABILITY_POINT_PROMPT = "point_prompt"
CAPABILITY_PROPAGATE = "propagate"


class SegmentationError(RuntimeError):
    """Raised with a message intended for the user, not just the log.

    The previous failure surfaced `remove bg requires ROUGH_CUT_ML_PROVIDER_URL`
    in the editor, which names an internal variable and tells the reader nothing
    they can act on. Anything raised here should say what is missing and how to
    supply it.
    """


@dataclass
class SegmentationResult:
    """Either a finished file on disk, or a URL a remote service already published.

    Providers must not upload anything. Storage is the job's concern — it already
    knows about Cloudinary, the uploads directory and the naming scheme, and
    duplicating that in each backend is how two of them end up disagreeing.
    """

    path: Path | None = None
    url: str | None = None

    def __post_init__(self) -> None:
        if (self.path is None) == (self.url is None):
            raise ValueError("SegmentationResult needs exactly one of path or url")


@runtime_checkable
class SegmentationProvider(Protocol):
    """One backend capable of producing mattes."""

    name: str

    def is_available(self) -> tuple[bool, str]:
        """`(ready, reason)`. `reason` is shown to the user when not ready."""

    def supports(self, capability: str) -> bool:
        """Whether this backend can do `capability`.

        The editor needs this to decide what to offer. Auto matting and
        point-prompt segmentation are genuinely different models, so a backend
        that can do one may not do the other — offering a subject picker that
        cannot segment is worse than not offering it.
        """

    def run_effect(
        self,
        source: str,
        effect_type: str,
        clip_target: dict[str, Any],
        settings: dict[str, Any],
        *,
        output_dir: Path,
        progress: Any = None,
    ) -> "SegmentationResult":
        """Run `effect_type` over `source`, writing any file into `output_dir`."""
