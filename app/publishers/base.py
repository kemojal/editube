from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.db.models import VideoPublication


class Publisher(ABC):
    """Platform-specific publish entrypoint (enqueue async work or complete inline)."""

    @abstractmethod
    def start_publish(self, db: "Session", publication: "VideoPublication") -> None:
        """Mutate `publication` and related rows; caller commits."""
        raise NotImplementedError
