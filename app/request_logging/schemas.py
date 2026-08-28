from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class LogMFAStepUpRequest(BaseModel):
    code: str = Field(min_length=6, max_length=16)


class LogSearchFilters(BaseModel):
    request_id: str | None = None
    method: str | None = None
    route: str | None = None
    status_code: int | None = None
    user_id: int | None = None
    workspace_id: int | None = None
    from_ts: datetime | None = None
    to_ts: datetime | None = None
