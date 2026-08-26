"""Upsert Stripe Product/Price rows from API objects or webhook payloads."""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import StripePrice, StripeProduct
from app.services.pricing import (
    SELF_SERVE_PLANS,
    is_editube_product,
    normalize_plan_key,
    resolve_plan_key,
)

logger = logging.getLogger(__name__)

_LOOKUP_KEY_RE = re.compile(
    r"^editube_(?P<plan>basic|pro|scale|free|enterprise)_(?P<iv>monthly|month|annual|annually|yearly|year)$",
    re.IGNORECASE,
)


def _meta_dict(obj: dict[str, Any]) -> dict[str, Any]:
    raw = obj.get("metadata")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        return dict(raw.items())  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        return {}


def parse_editube_mapping_from_price(price: dict[str, Any]) -> tuple[str | None, str | None]:
    """
    Resolve (editube_plan, editube_interval) from Stripe Price metadata or lookup_key.
    Interval is always 'month' or 'year' for subscriptions.
    """
    meta = _meta_dict(price)
    plan_raw = (meta.get("editube_plan") or meta.get("plan") or "").strip().lower() or None
    interval_raw = (meta.get("editube_interval") or meta.get("interval") or "").strip().lower() or None

    plan: str | None = None
    interval: str | None = None

    if plan_raw:
        # `resolve_plan_key` so an unrecognised value stays unrecognised;
        # `normalize_plan_key` would turn "premium" into "free" and map the
        # price to a tier nobody asked for.
        plan = resolve_plan_key(plan_raw)

    if interval_raw in ("month", "monthly"):
        interval = "month"
    elif interval_raw in ("year", "annual", "annually", "yearly"):
        interval = "year"

    lookup = (price.get("lookup_key") or "").strip()
    if lookup and (not plan or not interval):
        m = _LOOKUP_KEY_RE.match(lookup)
        if m:
            plan = resolve_plan_key(m.group("plan"))
            iv = m.group("iv").lower()
            if iv in ("monthly", "month"):
                interval = "month"
            elif iv in ("annual", "annually", "yearly", "year"):
                interval = "year"

    return plan, interval


def _product_id_from_price(price: dict[str, Any]) -> str | None:
    p = price.get("product")
    if isinstance(p, str):
        return p
    if isinstance(p, dict):
        return p.get("id")
    return getattr(p, "id", None) if p is not None else None


def ensure_stripe_product_row(
    db: Session,
    *,
    stripe_product_id: str,
    name: str | None = None,
    description: str | None = None,
    active: bool = True,
    metadata: dict[str, Any] | None = None,
) -> StripeProduct:
    row = (
        db.query(StripeProduct)
        .filter(StripeProduct.stripe_product_id == stripe_product_id)
        .first()
    )
    if row:
        if name is not None:
            row.name = name
        if description is not None:
            row.description = description
        row.active = active
        if metadata is not None:
            row.metadata_json = metadata
        return row
    row = StripeProduct(
        stripe_product_id=stripe_product_id,
        name=name,
        description=description,
        active=active,
        metadata_json=metadata or {},
    )
    db.add(row)
    # Flushed immediately, for two reasons that both bit in production.
    #
    # `SessionLocal` is built with `autoflush=False`, so the SELECT above does
    # not see rows added earlier in this same transaction. Without the flush,
    # syncing a product and then its price added the *same* product twice.
    #
    # And `stripe_prices.stripe_product_id` carries an FK to this table, so the
    # product must physically exist before the price INSERT runs — the catalog
    # sync died on `ForeignKeyViolation ... is not present in table
    # "stripe_products"` and imported nothing at all.
    db.flush()
    return row


def upsert_stripe_product_from_object(db: Session, obj: dict[str, Any]) -> StripeProduct | None:
    pid = obj.get("id")
    if not pid:
        return None
    return ensure_stripe_product_row(
        db,
        stripe_product_id=str(pid),
        name=obj.get("name"),
        description=obj.get("description"),
        active=bool(obj.get("active", True)),
        metadata=_meta_dict(obj),
    )


def upsert_stripe_price_from_object(db: Session, obj: dict[str, Any]) -> StripePrice | None:
    price_id = obj.get("id")
    if not price_id:
        return None
    product_id = _product_id_from_price(obj)
    if not product_id:
        logger.warning("stripe price missing product id: %s", price_id)
        return None

    product_row = ensure_stripe_product_row(db, stripe_product_id=str(product_id))

    recurring = obj.get("recurring") or {}
    stripe_interval = recurring.get("interval") if isinstance(recurring, dict) else None
    if stripe_interval not in ("month", "year", None):
        stripe_interval = None

    plan, mapped_interval = parse_editube_mapping_from_price(obj)
    interval = mapped_interval or stripe_interval
    if interval not in ("month", "year"):
        interval = None

    # Only products named "Editube" are ours. This Stripe account also carries
    # several unrelated products, and without the check a price of theirs that
    # happened to carry an `editube_plan` key — or a `lookup_key` the regex
    # liked — would map itself into our catalog and start granting tiers.
    # `None` means the product name has not synced yet, which is not a reason
    # to refuse: that would break a genuine price whose product webhook is
    # simply late.
    if plan is not None and is_editube_product(product_row.name) is False:
        logger.info(
            "ignoring editube_plan on price %s: product %r is not an Editube product",
            price_id,
            product_row.name,
        )
        plan = None

    row = (
        db.query(StripePrice).filter(StripePrice.stripe_price_id == str(price_id)).first()
    )
    currency = (obj.get("currency") or "").lower() or None
    unit_amount = obj.get("unit_amount")
    try:
        unit_amount_i = int(unit_amount) if unit_amount is not None else None
    except (TypeError, ValueError):
        unit_amount_i = None

    if row:
        row.stripe_product_id = str(product_id)
        row.currency = currency
        row.unit_amount = unit_amount_i
        row.nickname = obj.get("nickname")
        row.recurring_interval = interval
        row.active = bool(obj.get("active", True))
        row.metadata_json = _meta_dict(obj)
        row.editube_plan = plan
        row.editube_interval = interval if interval in ("month", "year") else None
        return row

    row = StripePrice(
        stripe_price_id=str(price_id),
        stripe_product_id=str(product_id),
        currency=currency,
        unit_amount=unit_amount_i,
        nickname=obj.get("nickname"),
        recurring_interval=interval if interval in ("month", "year") else None,
        active=bool(obj.get("active", True)),
        metadata_json=_meta_dict(obj),
        editube_plan=plan,
        editube_interval=interval if interval in ("month", "year") else None,
    )
    db.add(row)
    return row


def mark_stripe_product_inactive(db: Session, stripe_product_id: str) -> None:
    row = (
        db.query(StripeProduct)
        .filter(StripeProduct.stripe_product_id == stripe_product_id)
        .first()
    )
    if row:
        row.active = False


def mark_stripe_price_inactive(db: Session, stripe_price_id: str) -> None:
    row = (
        db.query(StripePrice).filter(StripePrice.stripe_price_id == stripe_price_id).first()
    )
    if row:
        row.active = False


def stripe_object_to_dict(obj: Any) -> dict[str, Any]:
    """Plain, deeply-converted dict from a Stripe object, dict, or mapping.

    Everything below this line indexes with `.get()`, so it needs real dicts
    all the way down. `StripeObject` is not a dict in stripe>=12 and has
    neither `.get()` nor `.items()`, and `to_dict_recursive` was removed in
    stripe 15 — the old implementation silently fell through to a shallow
    `to_dict()`, leaving nested `metadata` and `product` as StripeObjects that
    blew up on the next `.get()`.
    """
    return _deep_plain(obj) if not isinstance(obj, dict) else _deep_plain(dict(obj))


def _deep_plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _deep_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_deep_plain(v) for v in value]
    if isinstance(value, (str, bytes, int, float, bool)) or value is None:
        return value

    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _deep_plain(dict(to_dict()))
        except (TypeError, ValueError):
            pass
    # `StripeObject` keeps its payload on `_data`; reachable when `to_dict`
    # is absent or refuses.
    data = getattr(value, "_data", None)
    if isinstance(data, dict):
        return _deep_plain(data)
    try:
        return _deep_plain(dict(value))
    except (TypeError, ValueError):
        return value


def sync_catalog_from_stripe_api(db: Session) -> int:
    """
    List active recurring prices (with product expanded) and upsert local rows.
    Returns number of prices processed.
    """
    import stripe

    count = 0
    for price in stripe.Price.list(active=True, expand=["data.product"]).auto_paging_iter():
        d = stripe_object_to_dict(price)
        if not d.get("active", True):
            continue
        if not d.get("recurring"):
            continue
        try:
            prod = d.get("product")
            if isinstance(prod, dict):
                upsert_stripe_product_from_object(db, prod)
            upsert_stripe_price_from_object(db, d)
            db.commit()
            count += 1
        except Exception:
            logger.exception(
                "sync_catalog_from_stripe_api skipped price id=%s",
                d.get("id"),
            )
            db.rollback()
    return count


def resolve_checkout_price_id(db: Session, *, plan: str, interval: str) -> str | None:
    """Return Stripe Price id for checkout, or None if not synced."""
    canonical = normalize_plan_key(plan)
    if canonical not in SELF_SERVE_PLANS:
        return None
    if interval not in ("month", "year"):
        return None
    row = (
        db.query(StripePrice)
        .filter(
            StripePrice.editube_plan == canonical,
            StripePrice.editube_interval == interval,
            StripePrice.active.is_(True),
        )
        .order_by(StripePrice.id.desc())
        .first()
    )
    return row.stripe_price_id if row else None


def catalog_price_rows(db: Session) -> list[StripePrice]:
    return (
        db.query(StripePrice)
        .filter(
            StripePrice.active.is_(True),
            StripePrice.editube_plan.isnot(None),
            StripePrice.editube_interval.isnot(None),
        )
        .order_by(StripePrice.editube_plan, StripePrice.editube_interval)
        .all()
    )
