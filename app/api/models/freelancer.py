from pydantic import BaseModel
from datetime import datetime
from typing import Optional, Any, List, Dict


# --- Project scope settings ---

class ProjectScopeUpdate(BaseModel):
    scope_revisions_included: Optional[int] = None
    change_request_fee_cents: Optional[int] = None
    currency: Optional[str] = None
    hourly_rate_cents: Optional[int] = None
    deliverables_locked: Optional[bool] = None
    portfolio_public: Optional[bool] = None
    portfolio_slug: Optional[str] = None
    client_name: Optional[str] = None
    client_email: Optional[str] = None
    # Per-complexity hourly rates in cents, e.g. {"simple": 8000, "standard": 12000, "complex": 20000}
    rate_card_cents: Optional[Dict[str, int]] = None


class ProjectScopeResponse(BaseModel):
    id: int
    scope_revisions_included: int
    revision_count: int
    change_request_fee_cents: int
    currency: str
    hourly_rate_cents: Optional[int] = None
    deliverables_locked: bool
    portfolio_public: bool
    portfolio_slug: Optional[str] = None
    client_name: Optional[str] = None
    client_email: Optional[str] = None
    rate_card_cents: Optional[Dict[str, int]] = None

    class Config:
        orm_mode = True


# --- Revisions ---

class RevisionCreate(BaseModel):
    video_id: Optional[int] = None
    triggered_by: Optional[str] = None
    note: Optional[str] = None
    billable: bool = False


class RevisionResponse(BaseModel):
    id: int
    project_id: int
    video_id: Optional[int] = None
    round_number: int
    triggered_by: Optional[str] = None
    note: Optional[str] = None
    billable: bool
    created_at: datetime

    class Config:
        orm_mode = True


# --- Invoices ---

class InvoiceItemBody(BaseModel):
    description: str
    quantity: int = 1
    unit_price_cents: int = 0


class InvoiceItemResponse(BaseModel):
    id: int
    invoice_id: int
    description: str
    quantity: int
    unit_price_cents: int
    total_cents: int

    class Config:
        orm_mode = True


class InvoiceCreate(BaseModel):
    number: Optional[str] = None
    client_name: Optional[str] = None
    client_email: Optional[str] = None
    currency: str = "USD"
    tax_cents: int = 0
    due_at: Optional[datetime] = None
    notes: Optional[str] = None
    items: List[InvoiceItemBody] = []


class InvoiceUpdate(BaseModel):
    number: Optional[str] = None
    client_name: Optional[str] = None
    client_email: Optional[str] = None
    currency: Optional[str] = None
    tax_cents: Optional[int] = None
    due_at: Optional[datetime] = None
    notes: Optional[str] = None
    status: Optional[str] = None
    items: Optional[List[InvoiceItemBody]] = None


class InvoiceResponse(BaseModel):
    id: int
    project_id: int
    number: Optional[str] = None
    client_name: Optional[str] = None
    client_email: Optional[str] = None
    currency: str
    subtotal_cents: int
    tax_cents: int
    total_cents: int
    status: str
    stripe_invoice_id: Optional[str] = None
    stripe_payment_link: Optional[str] = None
    stripe_connect_account_id: Optional[str] = None
    due_at: Optional[datetime] = None
    sent_at: Optional[datetime] = None
    paid_at: Optional[datetime] = None
    notes: Optional[str] = None
    items: List[InvoiceItemResponse] = []
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True


# --- Milestones ---

class MilestoneCreate(BaseModel):
    name: str
    amount_cents: int = 0
    currency: str = "USD"
    percentage: Optional[int] = None
    due_at: Optional[datetime] = None
    order_index: int = 0


class MilestoneUpdate(BaseModel):
    name: Optional[str] = None
    amount_cents: Optional[int] = None
    currency: Optional[str] = None
    percentage: Optional[int] = None
    due_at: Optional[datetime] = None
    status: Optional[str] = None
    invoice_id: Optional[int] = None
    order_index: Optional[int] = None


class MilestoneResponse(BaseModel):
    id: int
    project_id: int
    name: str
    amount_cents: int
    currency: str
    percentage: Optional[int] = None
    due_at: Optional[datetime] = None
    status: str
    invoice_id: Optional[int] = None
    order_index: int
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True


# --- Contracts ---

class ContractCreate(BaseModel):
    title: str
    body: str
    signer_name: Optional[str] = None
    signer_email: Optional[str] = None


class ContractUpdate(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    signer_name: Optional[str] = None
    signer_email: Optional[str] = None
    status: Optional[str] = None


class ContractResponse(BaseModel):
    id: int
    project_id: int
    title: str
    body: str
    status: str
    signer_name: Optional[str] = None
    signer_email: Optional[str] = None
    signed_at: Optional[datetime] = None
    signing_token: Optional[str] = None
    pdf_url: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True


class ContractSendResponse(ContractResponse):
    email_sent: bool = False


class ContractSignBody(BaseModel):
    signer_name: str
    signer_email: str
    signature_data: str


class ContractPublicResponse(BaseModel):
    id: int
    title: str
    body: str
    status: str
    signer_name: Optional[str] = None
    signer_email: Optional[str] = None
    signed_at: Optional[datetime] = None
    pdf_url: Optional[str] = None


# --- Stripe Connect ---


class StripeConnectStatusResponse(BaseModel):
    stripe_connect_account_id: Optional[str] = None
    charges_enabled: bool = False
    details_submitted: bool = False
    payouts_enabled: bool = False
    platform_invoices_allowed: bool = False


class StripeConnectAccountResponse(BaseModel):
    stripe_connect_account_id: str
    created: bool


class StripeConnectAccountLinkResponse(BaseModel):
    url: str


# --- Time entries ---

class TimeEntryStart(BaseModel):
    note: Optional[str] = None
    billable: bool = True
    hourly_rate_cents: Optional[int] = None


class TimeEntryCreate(BaseModel):
    started_at: datetime
    ended_at: Optional[datetime] = None
    duration_seconds: int = 0
    note: Optional[str] = None
    billable: bool = True
    hourly_rate_cents: Optional[int] = None


class TimeEntryUpdate(BaseModel):
    ended_at: Optional[datetime] = None
    duration_seconds: Optional[int] = None
    note: Optional[str] = None
    billable: Optional[bool] = None


class TimeEntryResponse(BaseModel):
    id: int
    project_id: int
    user_id: Optional[int] = None
    started_at: datetime
    ended_at: Optional[datetime] = None
    duration_seconds: int
    note: Optional[str] = None
    billable: bool
    hourly_rate_cents: Optional[int] = None
    created_at: datetime

    class Config:
        orm_mode = True


# --- Estimates ---

class EstimateCreate(BaseModel):
    title: Optional[str] = None
    runtime_minutes: int = 0
    complexity: str = "standard"
    rate_cents_per_hour: int = 0
    line_items: Optional[Any] = None
    currency: str = "USD"


class EstimateResponse(BaseModel):
    id: int
    project_id: int
    title: Optional[str] = None
    runtime_minutes: int
    complexity: str
    rate_cents_per_hour: int
    estimated_hours: int
    line_items: Optional[Any] = None
    total_cents: int
    currency: str
    status: str
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True


# --- Portfolio (public) ---

class PortfolioVideo(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    thumbnail_url: Optional[str] = None
    duration: Optional[int] = None


class PortfolioResponse(BaseModel):
    slug: str
    project_name: str
    description: Optional[str] = None
    client_name: Optional[str] = None
    videos: List[PortfolioVideo]
