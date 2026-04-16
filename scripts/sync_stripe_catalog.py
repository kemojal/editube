#!/usr/bin/env python3
"""
One-shot Stripe catalog import into the local database.

Uses the same upsert path as webhooks and POST /billing/sync-catalog. Requires
DATABASE_URL and STRIPE_SECRET_KEY (loaded from .env via app.db.database).

Usage (from editube/):

  python scripts/sync_stripe_catalog.py
"""

from __future__ import annotations

import os
import sys

# Repo layout: editube/scripts/this_file.py → parent is package root
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import stripe  # noqa: E402

from app.db.database import SessionLocal  # noqa: E402
from app.services.stripe_catalog_sync import sync_catalog_from_stripe_api  # noqa: E402


def main() -> int:
    if not os.getenv("STRIPE_SECRET_KEY"):
        print("STRIPE_SECRET_KEY is required", file=sys.stderr)
        return 1
    stripe.api_key = os.environ["STRIPE_SECRET_KEY"]

    db = SessionLocal()
    try:
        n = sync_catalog_from_stripe_api(db)
        print(f"Synced {n} active recurring prices into the catalog.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
