#!/usr/bin/env python3
"""
Reconcile local entitlements against Stripe, and report or repair the drift.

Why this exists
---------------
Until the Aug 2026 billing audit, several paths granted `users.plan` without a
paying subscription behind it:

* `PUT /users/onboarding/plan` wrote the tier straight from a request body, so
  any authenticated account could award itself Scale;
* `customer.subscription.deleted` never downgraded, so every customer who ever
  cancelled kept their tier permanently;
* subscriptions in `incomplete` (first payment declined) were granted anyway.

Those paths are closed, but the rows they left behind are still in the
database, and they are *not* distinguishable from legitimate customers by
schema alone — plenty of real subscribers also have a paid `users.plan` with a
thin or missing `subscriptions` row, because that table postdates them.

Stripe is the only authority that can tell the two apart. This script asks it,
one customer at a time, and reports what it finds. Nothing is written unless
`--apply` is passed.

Usage (from editube/):

    # Report only. Always run this first, and read it.
    python scripts/reconcile_subscriptions.py

    # Same, as CSV for a spreadsheet or a ticket.
    python scripts/reconcile_subscriptions.py --format csv > drift.csv

    # Repair. Downgrades accounts Stripe says are not paying.
    python scripts/reconcile_subscriptions.py --apply

    # Repair a single account first, to see the shape of it.
    python scripts/reconcile_subscriptions.py --apply --user-id 4213

Requires DATABASE_URL and STRIPE_SECRET_KEY (loaded from .env by
app.db.database).
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Repo layout: editube/scripts/this_file.py → parent is package root
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import stripe  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db.database import SessionLocal  # noqa: E402
from app.db.models import StripePrice, Subscription, User  # noqa: E402
from app.services.entitlements import (  # noqa: E402
    ENTITLED_STATUSES,
    is_entitled,
    price_id_from_subscription,
    FOREIGN,
    resolve_subscription_plan,
    stripe_datetime,
    stripe_field,
    subscription_ownership,
    subscription_period,
)
from app.services.pricing import PAID_PLANS, resolve_plan_key  # noqa: E402

# Verdicts, worst first — this is also the report's sort order.
DOWNGRADE = "downgrade"          # local says paid, Stripe says nobody is paying
WRONG_TIER = "wrong_tier"        # paying, but for a different plan than granted
UPGRADE = "upgrade"              # paying for more than they were granted
MISSING_ROW = "missing_row"      # entitled, but no local subscriptions row
OK = "ok"

_SEVERITY = {DOWNGRADE: 0, WRONG_TIER: 1, UPGRADE: 2, MISSING_ROW: 3, OK: 4}


@dataclass
class Finding:
    user_id: int
    email: str
    local_plan: str
    stripe_plan: str | None
    stripe_status: str | None
    stripe_subscription_id: str | None
    verdict: str
    detail: str
    applied: bool = False

    def as_row(self) -> dict[str, object]:
        return {
            "user_id": self.user_id,
            "email": self.email,
            "local_plan": self.local_plan,
            "stripe_plan": self.stripe_plan or "",
            "stripe_status": self.stripe_status or "",
            "stripe_subscription_id": self.stripe_subscription_id or "",
            "verdict": self.verdict,
            "detail": self.detail,
            "applied": "yes" if self.applied else "",
        }


@dataclass
class Totals:
    scanned: int = 0
    by_verdict: dict[str, int] = field(default_factory=dict)
    stripe_errors: int = 0

    def count(self, verdict: str) -> None:
        self.by_verdict[verdict] = self.by_verdict.get(verdict, 0) + 1


def _live_stripe_subscription(db: Session, customer_id: str):
    """The *Editube* subscription Stripe considers current for this customer.

    Asks for everything rather than filtering server-side by status: a customer
    can hold several subscriptions (that was its own bug — checkout did not
    block duplicates), and the entitled one is not necessarily the newest.

    Subscriptions on prices we mirror but do not sell are skipped. One Stripe
    account here serves several unrelated products, and a customer of two of
    them has one `cus_` id for both — without this filter another product's
    subscription was being read as this user's Editube entitlement.
    """
    try:
        subs = stripe.Subscription.list(
            customer=customer_id, status="all", limit=100, expand=["data.items.data.price"]
        )
    except Exception as exc:  # noqa: BLE001 - reported per-user, never fatal
        raise RuntimeError(str(exc)) from exc

    entitled = [
        s
        for s in (getattr(subs, "data", None) or [])
        if is_entitled(stripe_field(s, "status"))
        and subscription_ownership(db, s) != FOREIGN
    ]
    if not entitled:
        return None
    # Prefer the most recently created among entitled ones.
    return max(entitled, key=lambda s: stripe_field(s, "created") or 0)


def _sync_row(db: Session, user: User, sub) -> None:
    """Mirror a Stripe subscription into `subscriptions`, creating it if absent."""
    sub_id = stripe_field(sub, "id")
    row = (
        db.query(Subscription)
        .filter(Subscription.stripe_subscription_id == sub_id)
        .first()
    )
    if not row:
        row = Subscription(user_id=user.id, stripe_subscription_id=sub_id)
        db.add(row)

    period_start, period_end = subscription_period(sub)
    row.user_id = user.id
    row.stripe_customer_id = user.stripe_customer_id
    row.customer_email = user.email
    row.stripe_price_id = price_id_from_subscription(sub)
    row.status = stripe_field(sub, "status")
    row.plan = resolve_subscription_plan(db, sub) or row.plan
    row.trial_start = stripe_datetime(stripe_field(sub, "trial_start"))
    row.current_period_start = period_start
    row.current_period_end = period_end
    row.cancel_at_period_end = bool(stripe_field(sub, "cancel_at_period_end"))
    row.ended_at = None


def _close_stale_rows(db: Session, user: User, keep_id: str | None) -> None:
    """Mark local rows ended when Stripe no longer considers them live."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    query = db.query(Subscription).filter(
        Subscription.user_id == user.id,
        Subscription.status.in_(sorted(ENTITLED_STATUSES)),
    )
    if keep_id:
        query = query.filter(Subscription.stripe_subscription_id != keep_id)
    for row in query.all():
        row.status = "canceled"
        row.ended_at = row.ended_at or now


def inspect(db: Session, user: User) -> Finding:
    local_plan = resolve_plan_key(user.plan) or "free"

    if not user.stripe_customer_id:
        return Finding(
            user_id=user.id,
            email=user.email or "",
            local_plan=local_plan,
            stripe_plan=None,
            stripe_status=None,
            stripe_subscription_id=None,
            verdict=DOWNGRADE,
            detail="no Stripe customer at all — never went through checkout",
        )

    sub = _live_stripe_subscription(db, user.stripe_customer_id)
    if sub is None:
        return Finding(
            user_id=user.id,
            email=user.email or "",
            local_plan=local_plan,
            stripe_plan=None,
            stripe_status=None,
            stripe_subscription_id=None,
            verdict=DOWNGRADE,
            detail="Stripe customer exists but holds no live Editube subscription",
        )

    status = stripe_field(sub, "status")
    stripe_plan = resolve_subscription_plan(db, sub)
    sub_id = stripe_field(sub, "id")

    if stripe_plan is None:
        # Naming the price matters: "not in the catalog" and "in the catalog
        # but carrying no editube_plan metadata" need completely different
        # fixes, and the first message could not tell them apart.
        price_id = price_id_from_subscription(sub)
        known = (
            db.query(StripePrice).filter(StripePrice.stripe_price_id == price_id).first()
            if price_id
            else None
        )
        if known is None:
            detail = f"price {price_id or '?'} is absent locally — run scripts/sync_stripe_catalog.py"
        else:
            detail = (
                f"price {price_id} is synced but has no editube_plan — set metadata "
                f"editube_plan/editube_interval (or a lookup_key) on it in Stripe"
            )
        return Finding(
            user_id=user.id, email=user.email or "", local_plan=local_plan,
            stripe_plan=None, stripe_status=status, stripe_subscription_id=sub_id,
            verdict=MISSING_ROW, detail=detail,
        )

    if stripe_plan == local_plan:
        row = (
            db.query(Subscription)
            .filter(Subscription.stripe_subscription_id == sub_id)
            .first()
        )
        if row is None:
            return Finding(
                user_id=user.id, email=user.email or "", local_plan=local_plan,
                stripe_plan=stripe_plan, stripe_status=status,
                stripe_subscription_id=sub_id, verdict=MISSING_ROW,
                detail="tier is correct; local subscriptions row is missing",
            )
        return Finding(
            user_id=user.id, email=user.email or "", local_plan=local_plan,
            stripe_plan=stripe_plan, stripe_status=status,
            stripe_subscription_id=sub_id, verdict=OK, detail="",
        )

    verdict = UPGRADE if local_plan == "free" else WRONG_TIER
    return Finding(
        user_id=user.id, email=user.email or "", local_plan=local_plan,
        stripe_plan=stripe_plan, stripe_status=status,
        stripe_subscription_id=sub_id, verdict=verdict,
        detail=f"granted {local_plan}, paying for {stripe_plan}",
    )


def repair(db: Session, user: User, finding: Finding) -> None:
    """Make the local record match Stripe. Caller commits."""
    if finding.verdict == OK:
        return

    if finding.verdict == DOWNGRADE:
        _close_stale_rows(db, user, keep_id=None)
        user.plan = "free"
        user.subscription_status = user.subscription_status or "canceled"
        user.stripe_subscription_id = None
        # The grace window is an allowance against the old cap; it does not
        # survive the tier that granted it.
        user.storage_grace_until = None
        finding.applied = True
        return

    if finding.stripe_plan is None:
        # MISSING_ROW with an unresolvable price. Writing a guess here would be
        # worse than leaving it: sync the catalog and run again.
        return

    sub = _live_stripe_subscription(db, user.stripe_customer_id)
    if sub is None:
        return
    _sync_row(db, user, sub)
    _close_stale_rows(db, user, keep_id=stripe_field(sub, "id"))
    if user.plan != finding.stripe_plan:
        user.storage_grace_until = None
    user.plan = finding.stripe_plan
    user.subscription_status = finding.stripe_status
    user.stripe_subscription_id = finding.stripe_subscription_id
    finding.applied = True


def candidates(db: Session, *, user_id: int | None, include_free: bool):
    q = db.query(User).filter(User.deleted_at.is_(None))
    if user_id is not None:
        return q.filter(User.id == user_id).all()
    if include_free:
        # Catches the other direction too: someone paying whose grant was lost.
        return q.filter(
            (User.plan.in_(sorted(PAID_PLANS))) | (User.stripe_customer_id.isnot(None))
        ).order_by(User.id).all()
    return q.filter(User.plan.in_(sorted(PAID_PLANS))).order_by(User.id).all()


def _print_table(findings: list[Finding]) -> None:
    if not findings:
        print("No drift found.")
        return
    headers = ["user_id", "email", "local", "stripe", "status", "verdict", "detail"]
    rows = [
        [
            str(f.user_id),
            (f.email or "")[:34],
            f.local_plan,
            f.stripe_plan or "—",
            f.stripe_status or "—",
            f.verdict + (" [applied]" if f.applied else ""),
            f.detail,
        ]
        for f in findings
    ]
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(line)
    print("-" * len(line))
    for r in rows:
        print("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)))


def _mapped_price_count(db: Session) -> int:
    return (
        db.query(StripePrice)
        .filter(
            StripePrice.active.is_(True),
            StripePrice.editube_plan.isnot(None),
            StripePrice.editube_interval.isnot(None),
        )
        .count()
    )


def _print_catalog_health() -> None:
    """How much of the synced catalog is actually usable for plan resolution.

    A Price with no `editube_plan` is invisible to both checkout and this
    report — it syncs fine and then resolves to nothing. Counting them here
    turns a per-customer mystery into one obvious line.
    """
    db = SessionLocal()
    try:
        total = db.query(StripePrice).filter(StripePrice.active.is_(True)).count()
        mapped = (
            db.query(StripePrice)
            .filter(
                StripePrice.active.is_(True),
                StripePrice.editube_plan.isnot(None),
                StripePrice.editube_interval.isnot(None),
            )
            .count()
        )
    finally:
        db.close()
    print(file=sys.stderr)
    print(f"catalog: {mapped}/{total} active prices carry an editube_plan mapping", file=sys.stderr)
    if mapped == 0:
        print(
            "  Nothing is mapped, so no price can be resolved to a plan. Set metadata\n"
            "  editube_plan + editube_interval (or a lookup_key like editube_pro_monthly)\n"
            "  in Stripe, then re-run scripts/sync_stripe_catalog.py.",
            file=sys.stderr,
        )
    elif mapped < total:
        print(
            f"  the other {total - mapped} are treated as belonging to another product on\n"
            "  this Stripe account and are ignored for entitlement. If one of them is\n"
            "  actually an Editube SKU, map it and re-sync.",
            file=sys.stderr,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the repairs. Without it, nothing is modified.",
    )
    parser.add_argument("--user-id", type=int, default=None, help="inspect one account")
    parser.add_argument(
        "--include-free",
        action="store_true",
        help="also check accounts on Free that have a Stripe customer (finds lost grants)",
    )
    parser.add_argument("--format", choices=("table", "csv"), default="table")
    parser.add_argument(
        "--show-ok", action="store_true", help="include accounts that already match"
    )
    args = parser.parse_args()

    if not os.getenv("STRIPE_SECRET_KEY"):
        print("STRIPE_SECRET_KEY is required", file=sys.stderr)
        return 1
    stripe.api_key = os.environ["STRIPE_SECRET_KEY"]

    db = SessionLocal()
    findings: list[Finding] = []
    totals = Totals()
    try:
        # With nothing mapped, every plan resolution falls back to subscription
        # metadata, and a subscription belonging to an entirely different
        # product on the same Stripe account is indistinguishable from one of
        # ours. Verdicts are still worth reading in that state; acting on them
        # is not, so --apply is refused rather than quietly trusted.
        if args.apply and _mapped_price_count(db) == 0:
            print(
                "Refusing --apply: no active price carries an editube_plan mapping, so\n"
                "this script cannot tell an Editube subscription from another product's\n"
                "on the same Stripe account. Map the prices, re-run\n"
                "scripts/sync_stripe_catalog.py, then apply.",
                file=sys.stderr,
            )
            return 2

        users = candidates(db, user_id=args.user_id, include_free=args.include_free)
        for user in users:
            totals.scanned += 1
            try:
                finding = inspect(db, user)
            except RuntimeError as exc:
                totals.stripe_errors += 1
                print(
                    f"  ! user {user.id}: Stripe lookup failed: {exc}",
                    file=sys.stderr,
                )
                continue

            totals.count(finding.verdict)
            if args.apply and finding.verdict != OK:
                repair(db, user, finding)
            if finding.verdict != OK or args.show_ok:
                findings.append(finding)

        if args.apply:
            db.commit()
    finally:
        db.close()

    findings.sort(key=lambda f: (_SEVERITY[f.verdict], f.user_id))

    if args.format == "csv":
        writer = csv.DictWriter(sys.stdout, fieldnames=list(findings[0].as_row())) if findings else None
        if writer:
            writer.writeheader()
            for f in findings:
                writer.writerow(f.as_row())
    else:
        _print_table(findings)

    _print_catalog_health()

    print(file=sys.stderr)
    print(f"scanned {totals.scanned} account(s)", file=sys.stderr)
    for verdict in (DOWNGRADE, WRONG_TIER, UPGRADE, MISSING_ROW, OK):
        n = totals.by_verdict.get(verdict, 0)
        if n:
            print(f"  {verdict:<12} {n}", file=sys.stderr)
    if totals.stripe_errors:
        print(f"  {'stripe_error':<12} {totals.stripe_errors}", file=sys.stderr)
    if not args.apply and any(f.verdict != OK for f in findings):
        print("\nDry run — nothing written. Re-run with --apply.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
