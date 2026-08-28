from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class ReviewWorkflowStageIn(BaseModel):
    stage_key: str
    label: str
    notify_user_ids: List[int] = Field(default_factory=list)


class ReviewWorkflowTemplateCreate(BaseModel):
    name: str
    stages: List[ReviewWorkflowStageIn] = Field(default_factory=list)


class ReviewWorkflowStageResponse(BaseModel):
    id: int
    template_id: int
    stage_index: int
    stage_key: str
    label: str
    notify_user_ids: List[int] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class ReviewWorkflowTemplateResponse(BaseModel):
    id: int
    project_id: int
    name: str
    created_at: datetime
    updated_at: datetime
    stages: List[ReviewWorkflowStageResponse] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class AttachWorkflowRunBody(BaseModel):
    template_id: int


class ReviewWorkflowRunResponse(BaseModel):
    id: int
    review_link_id: int
    template_id: int
    current_stage_index: int
    completed_at: Optional[datetime] = None
    total_stages: int = 0

    model_config = {"from_attributes": True}
