"""Freelancer Business Layer routes: scope-locked projects, revision
counter, invoicing (Stripe Connect stub), milestones, contracts with
e-sign token flow, time tracking, estimator, and public portfolio.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
import secrets

from app.db.database import get_db
from app.db.models import (
    Project,
    User,
    Video,
    ProjectRevision,
    Invoice,
    InvoiceItem,
    ProjectMilestone,
    Contract,
    TimeEntry,
    ProjectEstimate,
)
from app.utils.security import get_current_user
from app.api.models.freelancer import (
    ProjectScopeUpdate,
    ProjectScopeResponse,
    RevisionCreate,
    RevisionResponse,
    InvoiceCreate,
    InvoiceUpdate,
    InvoiceResponse,
    InvoiceItemResponse,
    MilestoneCreate,
    MilestoneUpdate,
    MilestoneResponse,
    ContractCreate,
    ContractUpdate,
    ContractResponse,
    ContractPublicResponse,
    ContractSignBody,
    TimeEntryStart,
    TimeEntryCreate,
    TimeEntryUpdate,
    TimeEntryResponse,
    EstimateCreate,
    EstimateResponse,
    PortfolioResponse,
    PortfolioVideo,
)


router = APIRouter(prefix="/freelancer", tags=["Freelancer"])
public_router = APIRouter(prefix="/public/freelancer", tags=["Freelancer-Public"])


def _owned(db: Session, project_id: int, user: User) -> Project:
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.creator_id != user.id:
        raise HTTPException(status_code=403, detail="Not authorized")
    return project


def _serialize_invoice(inv: Invoice) -> dict:
    return {
        "id": inv.id,
        "project_id": inv.project_id,
        "number": inv.number,
        "client_name": inv.client_name,
        "client_email": inv.client_email,
        "currency": inv.currency,
        "subtotal_cents": inv.subtotal_cents,
        "tax_cents": inv.tax_cents,
        "total_cents": inv.total_cents,
        "status": inv.status,
        "stripe_invoice_id": inv.stripe_invoice_id,
        "stripe_payment_link": inv.stripe_payment_link,
        "due_at": inv.due_at,
        "sent_at": inv.sent_at,
        "paid_at": inv.paid_at,
        "notes": inv.notes,
        "items": [InvoiceItemResponse.from_orm(i) for i in inv.items],
        "created_at": inv.created_at,
        "updated_at": inv.updated_at,
    }


# =====================================================================
# Scope settings
# =====================================================================

@router.get("/projects/{project_id}/scope", response_model=ProjectScopeResponse)
def get_scope(project_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return _owned(db, project_id, current_user)


@router.patch("/projects/{project_id}/scope", response_model=ProjectScopeResponse)
def update_scope(
    project_id: int,
    body: ProjectScopeUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = _owned(db, project_id, current_user)
    for k, v in body.dict(exclude_unset=True).items():
        setattr(project, k, v)
    db.commit()
    db.refresh(project)
    return project


# =====================================================================
# Revisions (counter)
# =====================================================================

@router.get("/projects/{project_id}/revisions", response_model=List[RevisionResponse])
def list_revisions(project_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _owned(db, project_id, current_user)
    return (
        db.query(ProjectRevision)
        .filter(ProjectRevision.project_id == project_id)
        .order_by(ProjectRevision.round_number.desc())
        .all()
    )


@router.post("/projects/{project_id}/revisions", response_model=RevisionResponse)
def create_revision(
    project_id: int,
    body: RevisionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = _owned(db, project_id, current_user)
    project.revision_count = (project.revision_count or 0) + 1
    over_scope = project.revision_count > (project.scope_revisions_included or 0)
    rev = ProjectRevision(
        project_id=project_id,
        video_id=body.video_id,
        round_number=project.revision_count,
        triggered_by=body.triggered_by,
        note=body.note,
        billable=body.billable or over_scope,
    )
    db.add(rev)
    db.commit()
    db.refresh(rev)
    return rev


@router.delete("/revisions/{revision_id}")
def delete_revision(revision_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    rev = db.query(ProjectRevision).filter(ProjectRevision.id == revision_id).first()
    if not rev:
        raise HTTPException(status_code=404, detail="Revision not found")
    _owned(db, rev.project_id, current_user)
    db.delete(rev)
    db.commit()
    return {"ok": True}


# =====================================================================
# Invoices
# =====================================================================

@router.get("/projects/{project_id}/invoices", response_model=List[InvoiceResponse])
def list_invoices(project_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _owned(db, project_id, current_user)
    invoices = (
        db.query(Invoice)
        .filter(Invoice.project_id == project_id)
        .order_by(Invoice.created_at.desc())
        .all()
    )
    return [_serialize_invoice(i) for i in invoices]


def _recalc_invoice(inv: Invoice) -> None:
    subtotal = 0
    for item in inv.items:
        item.total_cents = (item.quantity or 1) * (item.unit_price_cents or 0)
        subtotal += item.total_cents
    inv.subtotal_cents = subtotal
    inv.total_cents = subtotal + (inv.tax_cents or 0)


@router.post("/projects/{project_id}/invoices", response_model=InvoiceResponse)
def create_invoice(
    project_id: int,
    body: InvoiceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = _owned(db, project_id, current_user)
    inv = Invoice(
        project_id=project_id,
        created_by=current_user.id,
        number=body.number,
        client_name=body.client_name or project.client_name,
        client_email=body.client_email or project.client_email,
        currency=body.currency or project.currency or "USD",
        tax_cents=body.tax_cents or 0,
        due_at=body.due_at,
        notes=body.notes,
    )
    db.add(inv)
    db.flush()
    for it in body.items:
        db.add(
            InvoiceItem(
                invoice_id=inv.id,
                description=it.description,
                quantity=it.quantity or 1,
                unit_price_cents=it.unit_price_cents or 0,
                total_cents=(it.quantity or 1) * (it.unit_price_cents or 0),
            )
        )
    db.flush()
    db.refresh(inv)
    _recalc_invoice(inv)
    db.commit()
    db.refresh(inv)
    return _serialize_invoice(inv)


@router.patch("/invoices/{invoice_id}", response_model=InvoiceResponse)
def update_invoice(
    invoice_id: int,
    body: InvoiceUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    inv = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    _owned(db, inv.project_id, current_user)
    data = body.dict(exclude_unset=True)
    items = data.pop("items", None)
    for k, v in data.items():
        setattr(inv, k, v)
    if items is not None:
        for old in list(inv.items):
            db.delete(old)
        db.flush()
        for it in items:
            db.add(
                InvoiceItem(
                    invoice_id=inv.id,
                    description=it["description"],
                    quantity=it.get("quantity", 1),
                    unit_price_cents=it.get("unit_price_cents", 0),
                    total_cents=it.get("quantity", 1) * it.get("unit_price_cents", 0),
                )
            )
        db.flush()
        db.refresh(inv)
    _recalc_invoice(inv)
    db.commit()
    db.refresh(inv)
    return _serialize_invoice(inv)


@router.post("/invoices/{invoice_id}/send", response_model=InvoiceResponse)
def send_invoice(invoice_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    inv = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    _owned(db, inv.project_id, current_user)
    # TODO: call Stripe Invoices API / Stripe Connect to issue payment link.
    inv.status = "sent"
    inv.sent_at = datetime.utcnow()
    inv.stripe_payment_link = inv.stripe_payment_link or f"https://stripe.invalid/pay/{inv.id}"
    db.commit()
    db.refresh(inv)
    return _serialize_invoice(inv)


@router.post("/invoices/{invoice_id}/mark-paid", response_model=InvoiceResponse)
def mark_paid(invoice_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    inv = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    _owned(db, inv.project_id, current_user)
    inv.status = "paid"
    inv.paid_at = datetime.utcnow()
    db.commit()
    db.refresh(inv)
    return _serialize_invoice(inv)


@router.delete("/invoices/{invoice_id}")
def delete_invoice(invoice_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    inv = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    _owned(db, inv.project_id, current_user)
    db.delete(inv)
    db.commit()
    return {"ok": True}


# =====================================================================
# Milestones
# =====================================================================

@router.get("/projects/{project_id}/milestones", response_model=List[MilestoneResponse])
def list_milestones(project_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _owned(db, project_id, current_user)
    return (
        db.query(ProjectMilestone)
        .filter(ProjectMilestone.project_id == project_id)
        .order_by(ProjectMilestone.order_index.asc(), ProjectMilestone.created_at.asc())
        .all()
    )


@router.post("/projects/{project_id}/milestones", response_model=MilestoneResponse)
def create_milestone(
    project_id: int,
    body: MilestoneCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _owned(db, project_id, current_user)
    m = ProjectMilestone(project_id=project_id, **body.dict())
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


@router.patch("/milestones/{milestone_id}", response_model=MilestoneResponse)
def update_milestone(
    milestone_id: int,
    body: MilestoneUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    m = db.query(ProjectMilestone).filter(ProjectMilestone.id == milestone_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Milestone not found")
    _owned(db, m.project_id, current_user)
    for k, v in body.dict(exclude_unset=True).items():
        setattr(m, k, v)
    db.commit()
    db.refresh(m)
    return m


@router.delete("/milestones/{milestone_id}")
def delete_milestone(milestone_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    m = db.query(ProjectMilestone).filter(ProjectMilestone.id == milestone_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Milestone not found")
    _owned(db, m.project_id, current_user)
    db.delete(m)
    db.commit()
    return {"ok": True}


# =====================================================================
# Contracts (with public signing token)
# =====================================================================

@router.get("/projects/{project_id}/contracts", response_model=List[ContractResponse])
def list_contracts(project_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _owned(db, project_id, current_user)
    return (
        db.query(Contract)
        .filter(Contract.project_id == project_id)
        .order_by(Contract.created_at.desc())
        .all()
    )


@router.post("/projects/{project_id}/contracts", response_model=ContractResponse)
def create_contract(
    project_id: int,
    body: ContractCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _owned(db, project_id, current_user)
    c = Contract(
        project_id=project_id,
        title=body.title,
        body=body.body,
        signer_name=body.signer_name,
        signer_email=body.signer_email,
        signing_token=secrets.token_urlsafe(24),
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


@router.patch("/contracts/{contract_id}", response_model=ContractResponse)
def update_contract(
    contract_id: int,
    body: ContractUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    c = db.query(Contract).filter(Contract.id == contract_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Contract not found")
    _owned(db, c.project_id, current_user)
    for k, v in body.dict(exclude_unset=True).items():
        setattr(c, k, v)
    db.commit()
    db.refresh(c)
    return c


@router.post("/contracts/{contract_id}/send", response_model=ContractResponse)
def send_contract(contract_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    c = db.query(Contract).filter(Contract.id == contract_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Contract not found")
    _owned(db, c.project_id, current_user)
    if not c.signing_token:
        c.signing_token = secrets.token_urlsafe(24)
    c.status = "sent"
    # TODO: send e-mail to signer_email with public sign URL
    db.commit()
    db.refresh(c)
    return c


@router.delete("/contracts/{contract_id}")
def delete_contract(contract_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    c = db.query(Contract).filter(Contract.id == contract_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Contract not found")
    _owned(db, c.project_id, current_user)
    db.delete(c)
    db.commit()
    return {"ok": True}


# --- Public contract sign flow ---

@public_router.get("/contracts/{token}", response_model=ContractPublicResponse)
def public_get_contract(token: str, db: Session = Depends(get_db)):
    c = db.query(Contract).filter(Contract.signing_token == token).first()
    if not c:
        raise HTTPException(status_code=404, detail="Contract not found")
    return ContractPublicResponse(
        id=c.id,
        title=c.title,
        body=c.body,
        status=c.status,
        signer_name=c.signer_name,
        signer_email=c.signer_email,
        signed_at=c.signed_at,
    )


@public_router.post("/contracts/{token}/sign", response_model=ContractPublicResponse)
def public_sign_contract(token: str, body: ContractSignBody, db: Session = Depends(get_db)):
    c = db.query(Contract).filter(Contract.signing_token == token).first()
    if not c:
        raise HTTPException(status_code=404, detail="Contract not found")
    if c.signed_at:
        raise HTTPException(status_code=400, detail="Already signed")
    c.signer_name = body.signer_name
    c.signer_email = body.signer_email
    c.signature_data = body.signature_data
    c.signed_at = datetime.utcnow()
    c.status = "signed"
    # TODO: render PDF with signature stamp and upload to storage.
    db.commit()
    db.refresh(c)
    return ContractPublicResponse(
        id=c.id,
        title=c.title,
        body=c.body,
        status=c.status,
        signer_name=c.signer_name,
        signer_email=c.signer_email,
        signed_at=c.signed_at,
    )


# =====================================================================
# Time entries
# =====================================================================

@router.get("/projects/{project_id}/time", response_model=List[TimeEntryResponse])
def list_time(project_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _owned(db, project_id, current_user)
    return (
        db.query(TimeEntry)
        .filter(TimeEntry.project_id == project_id)
        .order_by(TimeEntry.started_at.desc())
        .all()
    )


@router.post("/projects/{project_id}/time/start", response_model=TimeEntryResponse)
def start_timer(
    project_id: int,
    body: TimeEntryStart,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = _owned(db, project_id, current_user)
    entry = TimeEntry(
        project_id=project_id,
        user_id=current_user.id,
        started_at=datetime.utcnow(),
        note=body.note,
        billable=body.billable,
        hourly_rate_cents=body.hourly_rate_cents or project.hourly_rate_cents,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


@router.post("/time/{entry_id}/stop", response_model=TimeEntryResponse)
def stop_timer(entry_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    entry = db.query(TimeEntry).filter(TimeEntry.id == entry_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Time entry not found")
    _owned(db, entry.project_id, current_user)
    if entry.ended_at:
        return entry
    entry.ended_at = datetime.utcnow()
    entry.duration_seconds = int((entry.ended_at - entry.started_at).total_seconds())
    db.commit()
    db.refresh(entry)
    return entry


@router.post("/projects/{project_id}/time", response_model=TimeEntryResponse)
def create_time_entry(
    project_id: int,
    body: TimeEntryCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _owned(db, project_id, current_user)
    entry = TimeEntry(project_id=project_id, user_id=current_user.id, **body.dict())
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


@router.patch("/time/{entry_id}", response_model=TimeEntryResponse)
def update_time_entry(
    entry_id: int,
    body: TimeEntryUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    entry = db.query(TimeEntry).filter(TimeEntry.id == entry_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Time entry not found")
    _owned(db, entry.project_id, current_user)
    for k, v in body.dict(exclude_unset=True).items():
        setattr(entry, k, v)
    db.commit()
    db.refresh(entry)
    return entry


@router.delete("/time/{entry_id}")
def delete_time_entry(entry_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    entry = db.query(TimeEntry).filter(TimeEntry.id == entry_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Time entry not found")
    _owned(db, entry.project_id, current_user)
    db.delete(entry)
    db.commit()
    return {"ok": True}


# =====================================================================
# Estimates
# =====================================================================

_COMPLEXITY_MULT = {"simple": 2.0, "standard": 4.0, "complex": 8.0}


@router.get("/projects/{project_id}/estimates", response_model=List[EstimateResponse])
def list_estimates(project_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _owned(db, project_id, current_user)
    return (
        db.query(ProjectEstimate)
        .filter(ProjectEstimate.project_id == project_id)
        .order_by(ProjectEstimate.created_at.desc())
        .all()
    )


@router.post("/projects/{project_id}/estimates", response_model=EstimateResponse)
def create_estimate(
    project_id: int,
    body: EstimateCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = _owned(db, project_id, current_user)
    mult = _COMPLEXITY_MULT.get(body.complexity, 4.0)
    est_minutes = (body.runtime_minutes or 0) * mult
    est_hours = max(1, int(round(est_minutes / 60.0)))
    rate = body.rate_cents_per_hour or project.hourly_rate_cents or 0
    total = est_hours * rate
    est = ProjectEstimate(
        project_id=project_id,
        title=body.title,
        runtime_minutes=body.runtime_minutes,
        complexity=body.complexity,
        rate_cents_per_hour=rate,
        estimated_hours=est_hours,
        line_items=body.line_items,
        total_cents=total,
        currency=body.currency or project.currency or "USD",
    )
    db.add(est)
    db.commit()
    db.refresh(est)
    return est


@router.delete("/estimates/{estimate_id}")
def delete_estimate(estimate_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    est = db.query(ProjectEstimate).filter(ProjectEstimate.id == estimate_id).first()
    if not est:
        raise HTTPException(status_code=404, detail="Estimate not found")
    _owned(db, est.project_id, current_user)
    db.delete(est)
    db.commit()
    return {"ok": True}


# =====================================================================
# Public portfolio
# =====================================================================

@public_router.get("/portfolio/{slug}", response_model=PortfolioResponse)
def public_portfolio(slug: str, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.portfolio_slug == slug).first()
    if not project or not project.portfolio_public:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    videos = db.query(Video).filter(Video.project_id == project.id).all()
    return PortfolioResponse(
        slug=slug,
        project_name=project.name,
        description=project.description,
        client_name=project.client_name,
        videos=[
            PortfolioVideo(
                id=v.id,
                name=v.name,
                description=v.description,
                thumbnail_url=getattr(v, "thumbnail_url", None),
                duration=getattr(v, "duration", None),
            )
            for v in videos
        ],
    )
