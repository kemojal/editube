"""HTTP provider — the contract this codebase already spoke.

Lifted verbatim from `rough_cut_effect._run_ml_provider` so an existing
deployment that already points at a provider service keeps working unchanged.
Only the error message is different, because the old one named an environment
variable at the user.
"""

from __future__ import annotations

import os
from typing import Any

from pathlib import Path

from .base import SegmentationError, SegmentationResult


class HttpSegmentationProvider:
    name = "http"

    def __init__(self) -> None:
        self.url = os.environ.get("ROUGH_CUT_ML_PROVIDER_URL", "").strip()
        self.timeout = float(os.environ.get("ROUGH_CUT_ML_PROVIDER_TIMEOUT", "900") or "900")

    def is_available(self) -> tuple[bool, str]:
        if not self.url:
            return False, (
                "No AI provider is configured for this server. Set "
                "SEGMENTATION_PROVIDER=local to process on this machine, or "
                "ROUGH_CUT_ML_PROVIDER_URL to use a remote service."
            )
        try:
            import httpx  # noqa: F401
        except Exception:
            return False, "The server is missing httpx, needed to reach the AI provider."
        return True, ""

    def run_effect(
        self,
        source: str,
        effect_type: str,
        clip_target: dict[str, Any],
        settings: dict[str, Any],
        *,
        output_dir: Path,
        progress: Any = None,
    ) -> SegmentationResult:
        ready, reason = self.is_available()
        if not ready:
            raise SegmentationError(reason)

        import httpx

        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                self.url,
                json={
                    "source": source,
                    "effectType": effect_type,
                    "clipTarget": clip_target,
                    "settings": settings,
                },
            )
            response.raise_for_status()
            data = response.json()

        output_url = data.get("outputUrl") or data.get("output_url")
        if not isinstance(output_url, str) or not output_url:
            raise SegmentationError("The AI provider returned no output.")
        return SegmentationResult(url=output_url)
