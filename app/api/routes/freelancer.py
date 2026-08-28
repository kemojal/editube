"""Freelancer Business Layer routes: scope-locked projects, revision
counter, invoicing (Stripe Connect Express + optional platform dev mode),
milestones, contracts with e-sign + PDF, time tracking, estimator, portfolio.
"""

import io
import os
import logging
import secrets
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

try:
    import stripe  # type: ignore
except Exception:  # pragma: no cover
    stripe = None  # type: ignore

logger = logging.getLogger(__name__)

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
from app.services.project_access import assert_manage_freelancer_financials
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
    ContractSendResponse,
    ContractPublicResponse,
    ContractSignBody,
    StripeConnectAccountLinkResponse,
    StripeConnectAccountResponse,
    StripeConnectStatusResponse,
    TimeEntryStart,
    TimeEntryCreate,
    TimeEntryUpdate,
    TimeEntryResponse,
    EstimateCreate,
    EstimateResponse,
    PortfolioResponse,
    PortfolioVideo,
)
from app.services.contract_pdf import build_signed_contract_pdf
from app.utils.email import send_transactional_email
from app.utils.return_path import safe_internal_path
from app.services.product_analytics import emit_once


router = APIRouter(prefix="/freelancer", tags=["Freelancer"])
public_router = APIRouter(prefix="/public/freelancer", tags=["Freelancer-Public"])


def _emit_project_event(
    db: Session,
    event_name: str,
    *,
    project: Project,
    event_id: str,
    user: User | None = None,
    user_id: int | None = None,
    source: str = "api",
    properties: dict | None = None,
) -> None:
    emit_once(
        db,
        event_name,
        event_id=event_id,
        user=user,
        user_id=user_id,
        workspace_id=project.workspace_id,
        source=source,
        properties={"project_id": project.id, **(properties or {})},
    )


def _emit_feature(
    db: Session,
    event_name: str,
    *,
    project: Project,
    feature_key: str,
    event_id: str,
    user: User | None = None,
    user_id: int | None = None,
    source: str = "api",
    result: str = "success",
    properties: dict | None = None,
) -> None:
    _emit_project_event(
        db,
        event_name,
        project=project,
        event_id=event_id,
        user=user,
        user_id=user_id,
        source=source,
        properties={"feature_key": feature_key, "result": result, **(properties or {})},
    )


def _owned(db: Session, project_id: int, user: User) -> Project:
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    assert_manage_freelancer_financials(db, user, project)
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
        "stripe_connect_account_id": getattr(inv, "stripe_connect_account_id", None),
        "due_at": inv.due_at,
        "sent_at": inv.sent_at,
        "paid_at": inv.paid_at,
        "notes": inv.notes,
        "items": [InvoiceItemResponse.from_orm(i) for i in inv.items],
        "created_at": inv.created_at,
        "updated_at": inv.updated_at,
    }


def _frontend_base() -> str:
    return os.getenv("FRONTEND_BASE_URL", "http://localhost:3000").rstrip("/")


def _allow_platform_invoices() -> bool:
    return os.getenv("FREELANCER_ALLOW_PLATFORM_INVOICES", "").lower() in ("1", "true", "yes")


def _connect_charges_ready(account: dict) -> bool:
    return bool(account.get("charges_enabled")) and bool(account.get("details_submitted"))


def _upload_contract_pdf_bytes(pdf_bytes: bytes, contract_id: int) -> Optional[str]:
    from app.storage import build_key, get_storage, storage_available

    if not storage_available():
        logger.warning("Storage backend not available; signed PDF not uploaded")
        return None
    try:
        folder = os.environ.get("CLOUDINARY_CONTRACTS_FOLDER", "contracts")
        key = build_key(
            folder=folder,
            public_id=f"signed_contract_{contract_id}.pdf",
            content_type="application/pdf",
        )
        return get_storage().upload_bytes(
            pdf_bytes, key=key, content_type="application/pdf"
        ).url
    except Exception:
        logger.exception("Contract PDF upload failed")
        return None


# =====================================================================
# Stripe Connect (Express)
# =====================================================================


@router.post("/stripe/connect/account", response_model=StripeConnectAccountResponse)
def stripe_connect_create_account(
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
):
    if not _stripe_key() or stripe is None:
        raise HTTPException(status_code=500, detail="Stripe is not configured")
    if current_user.stripe_connect_account_id:
        return StripeConnectAccountResponse(
            stripe_connect_account_id=current_user.stripe_connect_account_id,
            created=False,
        )
    country = (os.getenv("STRIPE_CONNECT_DEFAULT_COUNTRY") or "US").upper()
    acct = stripe.Account.create(
        type="express",
        country=country,
        email=current_user.email,
        business_type="individual",
        capabilities={"card_payments": {"requested": True}, "transfers": {"requested": True}},
    )
    current_user.stripe_connect_account_id = acct.id
    db.add(current_user)
    db.commit()
    db.refresh(current_user)
    return StripeConnectAccountResponse(stripe_connect_account_id=acct.id, created=True)


@router.post("/stripe/connect/account-link", response_model=StripeConnectAccountLinkResponse)
def stripe_connect_account_link(
    return_path: str = Query(default="/projects", max_length=512),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not _stripe_key() or stripe is None:
        raise HTTPException(status_code=500, detail="Stripe is not configured")
    if not current_user.stripe_connect_account_id:
        raise HTTPException(status_code=400, detail="Create a Connect account first")
    base = _frontend_base()
    ret = safe_internal_path(return_path)
    link = stripe.AccountLink.create(
        account=current_user.stripe_connect_account_id,
        refresh_url=f"{base}{ret}?stripe_connect=refresh",
        return_url=f"{base}{ret}?stripe_connect=return",
        type="account_onboarding",
    )
    return StripeConnectAccountLinkResponse(url=link.url)


@router.get("/stripe/connect/status", response_model=StripeConnectStatusResponse)
def stripe_connect_status(
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
):
    if not current_user.stripe_connect_account_id:
        return StripeConnectStatusResponse(platform_invoices_allowed=_allow_platform_invoices())
    if not _stripe_key() or stripe is None:
        return StripeConnectStatusResponse(
            stripe_connect_account_id=current_user.stripe_connect_account_id,
            platform_invoices_allowed=_allow_platform_invoices(),
        )
    try:
        acct = stripe.Account.retrieve(current_user.stripe_connect_account_id)
        return StripeConnectStatusResponse(
            stripe_connect_account_id=acct.id,
            charges_enabled=bool(acct.get("charges_enabled")),
            details_submitted=bool(acct.get("details_submitted")),
            payouts_enabled=bool(acct.get("payouts_enabled")),
            platform_invoices_allowed=_allow_platform_invoices(),
        )
    except Exception:
        logger.exception("Stripe Connect status failed")
        raise HTTPException(status_code=502, detail="Could not load Stripe Connect status") from None


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
    was_published = bool(project.portfolio_public and (project.portfolio_slug or "").strip())
    for k, v in body.dict(exclude_unset=True).items():
        setattr(project, k, v)
    is_published = bool(project.portfolio_public and (project.portfolio_slug or "").strip())
    if is_published and not was_published:
        _emit_feature(
            db,
            "feature_completed",
            project=project,
            feature_key="portfolio",
            event_id=f"feature:portfolio:project:{project.id}:published",
            user=current_user,
            properties={"completion_type": "public_portfolio_published"},
        )
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
    project = _owned(db, rev.project_id, current_user)
    pid = rev.project_id
    db.delete(rev)
    db.flush()
    mx = (
        db.query(func.max(ProjectRevision.round_number))
        .filter(ProjectRevision.project_id == pid)
        .scalar()
    )
    project.revision_count = int(mx or 0)
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
    _emit_project_event(
        db,
        "client_invoice_created",
        project=project,
        event_id=f"client-invoice:{inv.id}:created",
        user=current_user,
        properties={
            "invoice_id": inv.id,
            "currency": inv.currency,
            "amount_cents": inv.total_cents,
            "result": "success",
        },
    )
    _emit_feature(
        db,
        "feature_completed",
        project=project,
        feature_key="invoices",
        event_id=f"feature:invoices:invoice:{inv.id}:created",
        user=current_user,
        properties={"invoice_id": inv.id},
    )
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


def _stripe_key() -> Optional[str]:
    key = os.getenv("STRIPE_SECRET_KEY")
    if key and stripe is not None:
        stripe.api_key = key
        return key
    return None


def _mark_invoice_paid(
    db: Session,
    inv: Invoice,
    *,
    actor: User | None = None,
    source: str = "api",
) -> bool:
    """Idempotent — flip invoice + linked milestones + touch project."""
    if inv.status == "paid":
        return False
    project = db.query(Project).filter(Project.id == inv.project_id).first()
    if project is None:
        return False
    inv.status = "paid"
    inv.paid_at = datetime.utcnow()
    # Auto-complete any milestone linked to this invoice.
    linked = (
        db.query(ProjectMilestone)
        .filter(ProjectMilestone.invoice_id == inv.id)
        .all()
    )
    for m in linked:
        if m.status != "completed":
            m.status = "completed"
            _emit_project_event(
                db,
                "milestone_completed",
                project=project,
                event_id=f"milestone:{m.id}:completed",
                user=actor,
                user_id=None if actor else inv.created_by,
                source=source,
                properties={"milestone_id": m.id, "invoice_id": inv.id, "result": "success"},
            )
    _emit_project_event(
        db,
        "client_invoice_paid",
        project=project,
        event_id=f"client-invoice:{inv.id}:paid",
        user=actor,
        user_id=None if actor else inv.created_by,
        source=source,
        properties={
            "invoice_id": inv.id,
            "currency": inv.currency,
            "amount_cents": inv.total_cents,
            "result": "success",
        },
    )
    _emit_feature(
        db,
        "feature_result_used",
        project=project,
        feature_key="invoices",
        event_id=f"feature:invoices:invoice:{inv.id}:paid",
        user=actor,
        user_id=None if actor else inv.created_by,
        source=source,
        properties={"invoice_id": inv.id, "result_type": "payment"},
    )
    return True


@router.post("/invoices/{invoice_id}/send", response_model=InvoiceResponse)
def send_invoice(invoice_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    inv = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    project = _owned(db, inv.project_id, current_user)
    creator = db.query(User).filter(User.id == project.creator_id).first()
    connect_id = creator.stripe_connect_account_id if creator else None

    key = _stripe_key()
    if not key or stripe is None:
        raise HTTPException(status_code=400, detail="Stripe is not configured (set STRIPE_SECRET_KEY).")
    if not inv.client_email:
        raise HTTPException(status_code=400, detail="Invoice needs a client email before sending.")

    use_connect = False
    if connect_id:
        try:
            acct = stripe.Account.retrieve(connect_id)
            use_connect = _connect_charges_ready(dict(acct))
        except Exception:
            logger.exception("Could not retrieve Connect account %s", connect_id)

    if use_connect:
        kwargs = {"stripe_account": connect_id}
        try:
            customer = stripe.Customer.create(
                email=inv.client_email,
                name=inv.client_name or None,
                **kwargs,
            )
            for it in inv.items:
                stripe.InvoiceItem.create(
                    customer=customer.id,
                    amount=int(it.total_cents),
                    currency=(inv.currency or "USD").lower(),
                    description=(it.description or "")[:200],
                    **kwargs,
                )
            s_inv = stripe.Invoice.create(
                customer=customer.id,
                collection_method="send_invoice",
                days_until_due=14,
                metadata={"editube_invoice_id": str(inv.id), "project_id": str(inv.project_id)},
                **kwargs,
            )
            s_inv = stripe.Invoice.finalize_invoice(s_inv.id, **kwargs)
            try:
                stripe.Invoice.send_invoice(s_inv.id, **kwargs)
            except Exception:
                logger.exception("Stripe send_invoice failed for %s", s_inv.id)
            inv.stripe_invoice_id = s_inv.id
            inv.stripe_payment_link = getattr(s_inv, "hosted_invoice_url", None) or inv.stripe_payment_link
            inv.stripe_connect_account_id = connect_id
        except Exception as e:
            logger.exception("Stripe Connect invoice failed")
            raise HTTPException(
                status_code=502,
                detail=f"Could not create Stripe invoice on your connected account: {str(e)[:500]}",
            ) from e
    elif _allow_platform_invoices():
        try:
            customer = stripe.Customer.create(
                email=inv.client_email,
                name=inv.client_name or None,
            )
            for it in inv.items:
                stripe.InvoiceItem.create(
                    customer=customer.id,
                    amount=int(it.total_cents),
                    currency=(inv.currency or "USD").lower(),
                    description=(it.description or "")[:200],
                )
            s_inv = stripe.Invoice.create(
                customer=customer.id,
                collection_method="send_invoice",
                days_until_due=14,
                metadata={"editube_invoice_id": str(inv.id), "project_id": str(inv.project_id)},
            )
            s_inv = stripe.Invoice.finalize_invoice(s_inv.id)
            try:
                stripe.Invoice.send_invoice(s_inv.id)
            except Exception:
                logger.exception("Stripe send_invoice failed for %s", s_inv.id)
            inv.stripe_invoice_id = s_inv.id
            inv.stripe_payment_link = getattr(s_inv, "hosted_invoice_url", None) or inv.stripe_payment_link
            inv.stripe_connect_account_id = None
        except Exception:
            logger.exception("Stripe platform invoice failed; using stub link")
            inv.stripe_payment_link = inv.stripe_payment_link or f"https://stripe.invalid/pay/{inv.id}"
            inv.stripe_connect_account_id = None
    else:
        if connect_id:
            raise HTTPException(
                status_code=400,
                detail="Finish Stripe Connect onboarding before sending invoices, or set "
                "FREELANCER_ALLOW_PLATFORM_INVOICES=true for local development.",
            )
        raise HTTPException(
            status_code=400,
            detail="Create a Stripe Connect account (Business → Payments) before sending invoices.",
        )

    inv.status = "sent"
    inv.sent_at = datetime.utcnow()
    _emit_project_event(
        db,
        "client_invoice_sent",
        project=project,
        event_id=f"client-invoice:{inv.id}:sent",
        user=current_user,
        properties={"invoice_id": inv.id, "delivery_method": "stripe", "result": "success"},
    )
    _emit_feature(
        db,
        "feature_result_used",
        project=project,
        feature_key="invoices",
        event_id=f"feature:invoices:invoice:{inv.id}:sent",
        user=current_user,
        properties={"invoice_id": inv.id, "result_type": "invoice_sent"},
    )
    db.commit()
    db.refresh(inv)
    return _serialize_invoice(inv)


@router.post("/invoices/{invoice_id}/mark-paid", response_model=InvoiceResponse)
def mark_paid(invoice_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    inv = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    _owned(db, inv.project_id, current_user)
    _mark_invoice_paid(db, inv, actor=current_user)
    db.commit()
    db.refresh(inv)
    return _serialize_invoice(inv)


@router.post("/stripe/webhook")
async def freelancer_stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """Handle `invoice.paid` / `invoice.payment_succeeded` for freelancer
    invoices. Mounted at `/api/freelancer/stripe/webhook`.
    """
    if not _stripe_key():
        raise HTTPException(status_code=500, detail="Stripe not configured")
    wh_secret = os.getenv("STRIPE_WEBHOOK_SECRET")
    if not wh_secret:
        raise HTTPException(status_code=500, detail="Webhook secret not configured")
    payload = await request.body()
    sig = request.headers.get("stripe-signature")
    if not sig:
        raise HTTPException(status_code=400, detail="Missing stripe-signature header")
    try:
        event = stripe.Webhook.construct_event(payload, sig, wh_secret)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    etype = event["type"]
    obj = event["data"]["object"]
    connected_acct = event.get("account")
    if etype in ("invoice.paid", "invoice.payment_succeeded"):
        s_inv_id = obj.get("id")
        meta = obj.get("metadata") or {}
        inv: Optional[Invoice] = None
        local_id = meta.get("editube_invoice_id")
        if local_id:
            inv = db.query(Invoice).filter(Invoice.id == int(local_id)).first()
            if inv is not None and connected_acct and inv.stripe_connect_account_id:
                if inv.stripe_connect_account_id != connected_acct:
                    inv = None
        if inv is None and s_inv_id:
            q = db.query(Invoice).filter(Invoice.stripe_invoice_id == s_inv_id)
            if connected_acct:
                q = q.filter(Invoice.stripe_connect_account_id == connected_acct)
            inv = q.first()
        if inv is not None:
            _mark_invoice_paid(db, inv, source="stripe_webhook")
            db.commit()
    return {"received": True}


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


@router.post("/projects/{project_id}/milestones/seed-5050", response_model=List[MilestoneResponse])
def seed_50_50_milestones(
    project_id: int,
    total_cents: int,
    currency: str = "USD",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Seed two milestones: 50% deposit + 50% final. Idempotent — skips if
    any milestones already exist for the project.
    """
    project = _owned(db, project_id, current_user)
    existing = (
        db.query(ProjectMilestone)
        .filter(ProjectMilestone.project_id == project_id)
        .count()
    )
    if existing > 0:
        raise HTTPException(status_code=400, detail="Milestones already exist for this project")
    if total_cents <= 0:
        raise HTTPException(status_code=400, detail="total_cents must be positive")
    half = total_cents // 2
    final = total_cents - half
    currency = (currency or project.currency or "USD").upper()
    deposit = ProjectMilestone(
        project_id=project_id,
        name="Deposit (50%)",
        amount_cents=half,
        currency=currency,
        percentage=50,
        status="pending",
        order_index=0,
    )
    final_m = ProjectMilestone(
        project_id=project_id,
        name="Final payment (50%)",
        amount_cents=final,
        currency=currency,
        percentage=50,
        status="pending",
        order_index=1,
    )
    db.add(deposit)
    db.add(final_m)
    db.flush()
    for milestone in (deposit, final_m):
        _emit_project_event(
            db,
            "milestone_created",
            project=project,
            event_id=f"milestone:{milestone.id}:created",
            user=current_user,
            properties={
                "milestone_id": milestone.id,
                "currency": milestone.currency,
                "amount_cents": milestone.amount_cents,
                "result": "success",
            },
        )
    _emit_feature(
        db,
        "feature_completed",
        project=project,
        feature_key="milestones",
        event_id=f"feature:milestones:project:{project.id}:seed-5050",
        user=current_user,
        properties={"milestone_count": 2, "creation_method": "seed_5050"},
    )
    db.commit()
    db.refresh(deposit)
    db.refresh(final_m)
    return [deposit, final_m]


@router.post("/projects/{project_id}/milestones", response_model=MilestoneResponse)
def create_milestone(
    project_id: int,
    body: MilestoneCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = _owned(db, project_id, current_user)
    m = ProjectMilestone(project_id=project_id, **body.dict())
    db.add(m)
    db.flush()
    _emit_project_event(
        db,
        "milestone_created",
        project=project,
        event_id=f"milestone:{m.id}:created",
        user=current_user,
        properties={
            "milestone_id": m.id,
            "currency": m.currency,
            "amount_cents": m.amount_cents,
            "result": "success",
        },
    )
    _emit_feature(
        db,
        "feature_completed",
        project=project,
        feature_key="milestones",
        event_id=f"feature:milestones:milestone:{m.id}:created",
        user=current_user,
        properties={"milestone_id": m.id, "creation_method": "manual"},
    )
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
    project = _owned(db, m.project_id, current_user)
    previous_status = m.status
    for k, v in body.dict(exclude_unset=True).items():
        setattr(m, k, v)
    if previous_status != "completed" and m.status == "completed":
        _emit_project_event(
            db,
            "milestone_completed",
            project=project,
            event_id=f"milestone:{m.id}:completed",
            user=current_user,
            properties={"milestone_id": m.id, "completion_method": "manual", "result": "success"},
        )
        _emit_feature(
            db,
            "feature_result_used",
            project=project,
            feature_key="milestones",
            event_id=f"feature:milestones:milestone:{m.id}:completed",
            user=current_user,
            properties={"milestone_id": m.id, "result_type": "completed"},
        )
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
    project = _owned(db, project_id, current_user)
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
    db.flush()
    _emit_feature(
        db,
        "feature_completed",
        project=project,
        feature_key="contracts",
        event_id=f"feature:contracts:contract:{c.id}:created",
        user=current_user,
        properties={"contract_id": c.id},
    )
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
    project = _owned(db, c.project_id, current_user)
    for k, v in body.dict(exclude_unset=True).items():
        setattr(c, k, v)
    db.commit()
    db.refresh(c)
    return c


@router.post("/contracts/{contract_id}/send", response_model=ContractSendResponse)
def send_contract(contract_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    c = db.query(Contract).filter(Contract.id == contract_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Contract not found")
    _owned(db, c.project_id, current_user)
    if not c.signing_token:
        c.signing_token = secrets.token_urlsafe(24)
    c.status = "sent"
    sign_url = f"{_frontend_base()}/contract/{c.signing_token}"
    email_sent = False
    to_addr = (c.signer_email or "").strip()
    if to_addr:
        email_sent = send_transactional_email(
            to_addr,
            f"Contract to sign: {c.title}",
            f"Please open this link to review and sign:\n{sign_url}\n",
            f"<p>Please <a href=\"{sign_url}\">open this link</a> to review and sign.</p>",
        )
    _emit_project_event(
        db,
        "contract_sent",
        project=project,
        event_id=f"contract:{c.id}:sent",
        user=current_user,
        properties={"contract_id": c.id, "email_delivered": email_sent, "result": "success"},
    )
    _emit_feature(
        db,
        "feature_result_used",
        project=project,
        feature_key="contracts",
        event_id=f"feature:contracts:contract:{c.id}:sent",
        user=current_user,
        properties={"contract_id": c.id, "result_type": "contract_sent"},
    )
    db.commit()
    db.refresh(c)
    if hasattr(ContractResponse, "model_validate"):
        base = ContractResponse.model_validate(c).model_dump()
    else:
        base = ContractResponse.from_orm(c).dict()
    base["email_sent"] = email_sent
    return ContractSendResponse(**base)


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
        pdf_url=c.pdf_url,
    )


@public_router.post("/contracts/{token}/sign", response_model=ContractPublicResponse)
def public_sign_contract(token: str, body: ContractSignBody, db: Session = Depends(get_db)):
    c = db.query(Contract).filter(Contract.signing_token == token).first()
    if not c:
        raise HTTPException(status_code=404, detail="Contract not found")
    if c.signed_at:
        raise HTTPException(status_code=400, detail="Already signed")
    project = db.query(Project).filter(Project.id == c.project_id).first()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    c.signer_name = body.signer_name
    c.signer_email = body.signer_email
    c.signature_data = body.signature_data
    c.signed_at = datetime.utcnow()
    c.status = "signed"
    try:
        pdf_bytes = build_signed_contract_pdf(
            title=c.title,
            body=c.body,
            signer_name=body.signer_name,
            signer_email=body.signer_email,
            signature_data=body.signature_data,
            signed_at=c.signed_at,
        )
        url = _upload_contract_pdf_bytes(pdf_bytes, c.id)
        if url:
            c.pdf_url = url
    except Exception:
        logger.exception("Signed contract PDF generation failed")
    _emit_project_event(
        db,
        "contract_signed",
        project=project,
        event_id=f"contract:{c.id}:signed",
        properties={"contract_id": c.id, "actor_type": "guest", "result": "success"},
    )
    _emit_feature(
        db,
        "feature_result_used",
        project=project,
        feature_key="contracts",
        event_id=f"feature:contracts:contract:{c.id}:signed",
        properties={"contract_id": c.id, "actor_type": "guest", "result_type": "contract_signed"},
    )
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
        pdf_url=c.pdf_url,
    )


# =====================================================================
# Time entries
# =====================================================================

@router.get("/projects/{project_id}/time", response_model=List[TimeEntryResponse])
def list_time(project_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    project = _owned(db, project_id, current_user)
    rows = (
        db.query(TimeEntry)
        .filter(TimeEntry.project_id == project_id)
        .order_by(TimeEntry.started_at.desc())
        .all()
    )
    if rows:
        _emit_feature(
            db,
            "feature_result_used",
            project=project,
            feature_key="time_tracking",
            event_id=f"feature:time-tracking:project:{project.id}:report-opened",
            user=current_user,
            properties={"result_type": "time_report_opened", "entry_count": len(rows)},
        )
        db.commit()
    return rows


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
    db.flush()
    _emit_project_event(
        db,
        "time_entry_started",
        project=project,
        event_id=f"time-entry:{entry.id}:started",
        user=current_user,
        properties={"time_entry_id": entry.id, "billable": entry.billable, "result": "success"},
    )
    _emit_feature(
        db,
        "feature_started",
        project=project,
        feature_key="time_tracking",
        event_id=f"feature:time-tracking:entry:{entry.id}:started",
        user=current_user,
        properties={"time_entry_id": entry.id, "entry_type": "timer"},
    )
    db.commit()
    db.refresh(entry)
    return entry


@router.post("/time/{entry_id}/stop", response_model=TimeEntryResponse)
def stop_timer(entry_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    entry = db.query(TimeEntry).filter(TimeEntry.id == entry_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Time entry not found")
    project = _owned(db, entry.project_id, current_user)
    if entry.ended_at:
        return entry
    entry.ended_at = datetime.utcnow()
    entry.duration_seconds = int((entry.ended_at - entry.started_at).total_seconds())
    _emit_project_event(
        db,
        "time_entry_stopped",
        project=project,
        event_id=f"time-entry:{entry.id}:stopped",
        user=current_user,
        properties={
            "time_entry_id": entry.id,
            "billable": entry.billable,
            "duration_seconds": entry.duration_seconds,
            "result": "success",
        },
    )
    _emit_feature(
        db,
        "feature_completed",
        project=project,
        feature_key="time_tracking",
        event_id=f"feature:time-tracking:entry:{entry.id}:completed",
        user=current_user,
        properties={"time_entry_id": entry.id, "entry_type": "timer"},
    )
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
    project = _owned(db, project_id, current_user)
    entry = TimeEntry(project_id=project_id, user_id=current_user.id, **body.dict())
    db.add(entry)
    db.flush()
    _emit_project_event(
        db,
        "time_entry_stopped",
        project=project,
        event_id=f"time-entry:{entry.id}:manual",
        user=current_user,
        properties={
            "time_entry_id": entry.id,
            "billable": entry.billable,
            "duration_seconds": entry.duration_seconds,
            "entry_type": "manual",
            "result": "success",
        },
    )
    _emit_feature(
        db,
        "feature_completed",
        project=project,
        feature_key="time_tracking",
        event_id=f"feature:time-tracking:entry:{entry.id}:manual",
        user=current_user,
        properties={"time_entry_id": entry.id, "entry_type": "manual"},
    )
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
    project = _owned(db, project_id, current_user)
    rows = (
        db.query(ProjectEstimate)
        .filter(ProjectEstimate.project_id == project_id)
        .order_by(ProjectEstimate.created_at.desc())
        .all()
    )
    if rows:
        _emit_feature(
            db,
            "feature_result_used",
            project=project,
            feature_key="estimates",
            event_id=f"feature:estimates:project:{project.id}:saved-estimates-opened",
            user=current_user,
            properties={"result_type": "saved_estimates_opened", "estimate_count": len(rows)},
        )
        db.commit()
    return rows


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
    rc = project.rate_card_cents or {}
    ck = (body.complexity or "standard").lower()
    rate_from_card = None
    if isinstance(rc, dict):
        if ck in rc:
            rate_from_card = rc.get(ck)
        elif body.complexity in rc:
            rate_from_card = rc.get(body.complexity)
    if rate_from_card is not None:
        rate = int(rate_from_card)
    else:
        rate = int(body.rate_cents_per_hour or project.hourly_rate_cents or 0)
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
    db.flush()
    _emit_project_event(
        db,
        "estimate_created",
        project=project,
        event_id=f"estimate:{est.id}:created",
        user=current_user,
        properties={
            "estimate_id": est.id,
            "currency": est.currency,
            "amount_cents": est.total_cents,
            "complexity": est.complexity,
            "result": "success",
        },
    )
    _emit_feature(
        db,
        "feature_completed",
        project=project,
        feature_key="estimates",
        event_id=f"feature:estimates:estimate:{est.id}:created",
        user=current_user,
        properties={"estimate_id": est.id},
    )
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
    videos = (
        db.query(Video)
        .filter(Video.project_id == project.id, Video.status == "approved")
        .all()
    )
    view_id = secrets.token_hex(12)
    _emit_project_event(
        db,
        "portfolio_viewed",
        project=project,
        event_id=f"portfolio:{project.id}:view:{view_id}",
        properties={"feature_key": "portfolio", "video_count": len(videos), "result": "success"},
    )
    _emit_feature(
        db,
        "feature_opened",
        project=project,
        feature_key="portfolio",
        event_id=f"feature:portfolio:project:{project.id}:view:{view_id}",
        properties={"video_count": len(videos), "actor_type": "visitor"},
    )
    _emit_feature(
        db,
        "feature_result_used",
        project=project,
        feature_key="portfolio",
        event_id=f"feature:portfolio:project:{project.id}:result-view:{view_id}",
        properties={
            "video_count": len(videos),
            "actor_type": "visitor",
            "result_type": "public_portfolio_viewed",
        },
    )
    db.commit()
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
