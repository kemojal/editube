#!/usr/bin/env python3
"""
Give every user a personal workspace.

Accounts created before signup started calling `ensure_personal_workspace` have
no `workspace_members` row at all. Everything keyed off membership then fails
for them: `GET /billing/usage` 404s, `GET /workspaces` returns [], and the
account dialog shows "No workspace found" next to a live Pro subscription.

The read paths self-heal now (`billing.get_billing_usage`,
`workspaces.list_my_workspaces`), but that only repairs an account once someone
opens the right panel. This repairs them all up front.

Safe to re-run: `ensure_personal_workspace` returns the existing owner
workspace when there is one, so a second pass reports zero created.

Usage:
  python scripts/backfill_personal_workspaces.py --dry-run
  python scripts/backfill_personal_workspaces.py
  python scripts/backfill_personal_workspaces.py --limit 50
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db.database import SessionLocal  # noqa: E402
from app.db.models import User, WorkspaceMember  # noqa: E402
from app.services.workspace_bootstrap import ensure_personal_workspace  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List the accounts that would get a workspace, write nothing.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the first N accounts (useful for a cautious first run).",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        # Users with no membership row of any kind — not just no owned
        # workspace. Someone who was invited into a team as an editor already
        # has a workspace to read usage against and must not get a second one.
        members = db.query(WorkspaceMember.user_id).distinct().subquery()
        query = (
            db.query(User)
            .filter(~User.id.in_(db.query(members.c.user_id)))
            .order_by(User.id.asc())
        )
        if args.limit:
            query = query.limit(args.limit)
        orphans = query.all()

        if not orphans:
            print("All accounts already belong to a workspace. Nothing to do.")
            return 0

        print(f"{len(orphans)} account(s) without a workspace:")
        created = 0
        failed = 0

        for user in orphans:
            label = f"  #{user.id} {user.email or '(no email)'}"
            if args.dry_run:
                print(f"{label} — would create")
                continue
            try:
                ws = ensure_personal_workspace(db, user)
                created += 1
                print(f"{label} → workspace #{ws.id} {ws.name!r}")
            except Exception as exc:  # keep going; one bad row shouldn't stop the run
                db.rollback()
                failed += 1
                print(f"{label} — FAILED: {exc}", file=sys.stderr)

        if args.dry_run:
            print(f"\nDry run — {len(orphans)} account(s) would be repaired. Nothing written.")
        else:
            print(f"\nCreated {created} workspace(s), {failed} failure(s).")
        return 1 if failed else 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
