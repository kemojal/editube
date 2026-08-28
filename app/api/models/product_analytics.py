from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from typing import Literal


class AnalyticsConfigResponse(BaseModel):
    consent_version: str
    region_policy: str
    product_analytics_configured: bool
    replay_default_enabled: bool = False
    privacy_notice_path: str = "/legal/privacy"
    cookie_notice_path: str = "/legal/cookies"


class AnalyticsConsentUpdate(BaseModel):
    anonymous_consent_id: str = Field(min_length=16, max_length=128)
    analytics_enabled: bool
    replay_enabled: bool = False
    product_data_improvement_enabled: bool = False
    consent_version: str = Field(min_length=1, max_length=80)
    region_policy: str = Field(default="default", min_length=1, max_length=40)
    global_privacy_control: bool = False


class AnalyticsConsentResponse(BaseModel):
    anonymous_consent_id: str
    consent_state: str
    analytics_enabled: bool
    replay_enabled: bool
    product_data_improvement_enabled: bool
    consent_version: str
    region_policy: str
    global_privacy_control: bool
    consented_at: datetime | None = None
    withdrawn_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class AnalyticsFeedbackCreate(BaseModel):
    prompt_key: str = Field(min_length=1, max_length=80)
    reason_code: str = Field(min_length=1, max_length=80)
    comment: str | None = Field(default=None, max_length=2000)
    route_template: str | None = Field(default=None, max_length=200)
    feature_key: str | None = Field(default=None, max_length=80)
    analytics_session_id: str | None = Field(default=None, max_length=128)
    workspace_id: int | None = None
    consent_version: str | None = Field(default=None, max_length=80)
    user_initiated: bool = False


class AnalyticsFeedbackResponse(BaseModel):
    id: int
    accepted: bool = True
    comment_saved: bool


class ProjectSetupFailureCreate(BaseModel):
    project_id: int = Field(gt=0)
    step_key: Literal["media", "video", "auto-edit", "clips", "unknown"]
    error_code: Literal["project_setup_failed"] = "project_setup_failed"


class DashboardFirstViewResponse(BaseModel):
    first_view: bool
    viewed_at: datetime
    time_since_account_creation_ms: int


class AnalyticsDeletionResponse(BaseModel):
    request_id: int
    status: str
    provider_status: dict[str, str] | None = None
    requested_at: datetime | None = None
    completed_at: datetime | None = None
