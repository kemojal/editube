"""Workspace-scoped UGC render credits.

Append-only ledger (``aiugc.ugc_credit_ledger``); balance = sum(delta).
Model: generation **reserves** (debits) N credits up front; a failed render
**refunds** 1. Monthly allotment is granted lazily per plan, idempotent per
calendar month via the ``period`` column.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import UgcCreditLedger, User, Workspace
from app.services.pricing import get_plan_spec
from app.services.product_analytics import emit_after_commit


def credit_cost_per_variation() -> int:
    try:
        return max(0, int(os.environ.get("UGC_CREDIT_COST_PER_VARIATION", "1")))
    except ValueError:
        return 1


def _current_period() -> str:
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


def balance(db: Session, workspace_id: int) -> int:
    total = (
        db.query(func.coalesce(func.sum(UgcCreditLedger.delta), 0))
        .filter(UgcCreditLedger.workspace_id == workspace_id)
        .scalar()
    )
    return int(total or 0)


def _append(
    db: Session,
    workspace_id: int,
    delta: int,
    reason: str,
    *,
    variation_id: int | None = None,
    period: str | None = None,
) -> UgcCreditLedger:
    new_balance = balance(db, workspace_id) + delta
    row = UgcCreditLedger(
        workspace_id=workspace_id,
        delta=delta,
        reason=reason,
        variation_id=variation_id,
        period=period,
        balance_after=new_balance,
    )
    db.add(row)
    db.flush()
    return row


def _workspace_plan(db: Session, workspace_id: int) -> str:
    ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    if not ws:
        return "free"
    owner = db.query(User).filter(User.id == ws.owner_user_id).first()
    return (owner.plan if owner else None) or "free"


def ensure_monthly_grant(db: Session, workspace_id: int) -> None:
    """Grant this month's plan allotment once per calendar month."""
    period = _current_period()
    already = (
        db.query(UgcCreditLedger)
        .filter(
            UgcCreditLedger.workspace_id == workspace_id,
            UgcCreditLedger.reason == "monthly_grant",
            UgcCreditLedger.period == period,
        )
        .first()
    )
    if already:
        return
    allotment = int(getattr(get_plan_spec(_workspace_plan(db, workspace_id)), "ugc_credits_monthly", 0) or 0)
    if allotment > 0:
        _append(db, workspace_id, allotment, "monthly_grant", period=period)
        db.commit()


def reserve(db: Session, workspace_id: int, amount: int) -> bool:
    """Debit ``amount`` up front if affordable. Returns False if insufficient."""
    if amount <= 0:
        return True
    ensure_monthly_grant(db, workspace_id)
    available = balance(db, workspace_id)
    if available < amount:
        ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
        if ws is not None:
            emit_after_commit(
                "quota_threshold_reached",
                event_id=f"quota:ugc_credits:{workspace_id}:{_current_period()}:{available}",
                user_id=ws.owner_user_id,
                workspace_id=workspace_id,
                properties={
                    "quota_key": "ugc_credits",
                    "threshold_percent": 100,
                    "used": max(0, amount - available),
                    "cap": available,
                    "needed": amount,
                    "result": "blocked",
                },
            )
        return False
    _append(db, workspace_id, -amount, "reserve")
    db.commit()
    return True


def refund(db: Session, workspace_id: int, amount: int = 1, *, variation_id: int | None = None) -> None:
    if amount <= 0:
        return
    _append(db, workspace_id, amount, "refund", variation_id=variation_id)
    db.commit()


def topup(db: Session, workspace_id: int, amount: int) -> None:
    if amount <= 0:
        return
    _append(db, workspace_id, amount, "topup")
    db.commit()
