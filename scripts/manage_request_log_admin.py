#!/usr/bin/env python3
"""Grant/revoke internal request-log access using the migration-owner session."""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import connect_args_for
from app.db.log_models import LogAccessEvent, LogAdminAccessGrant
from app.db.models import User, UserMFAMethod


INTERNAL_ROLES = {"admin", "internal_admin", "super_admin"}


def _database_url() -> str:
    url = (os.getenv("LOG_MIGRATION_DATABASE_URL") or os.getenv("DATABASE_URL") or "").strip()
    if not url:
        raise SystemExit("Set LOG_MIGRATION_DATABASE_URL (preferred) or DATABASE_URL")
    return url


def _user(db, email: str) -> User:  # noqa: ANN001
    row = db.query(User).filter(User.email == email.strip().lower(), User.deleted_at.is_(None)).first()
    if not row:
        raise SystemExit(f"User not found: {email}")
    if row.role not in INTERNAL_ROLES:
        raise SystemExit(f"Refusing access: {email} has non-internal role {row.role!r}")
    verified_mfa = (
        db.query(UserMFAMethod)
        .filter(
            UserMFAMethod.user_id == row.id,
            UserMFAMethod.verified_at.isnot(None),
            UserMFAMethod.disabled_at.is_(None),
        )
        .first()
    )
    if not verified_mfa:
        raise SystemExit(f"Refusing access: {email} does not have verified MFA")
    return row


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    grant = subparsers.add_parser("grant")
    grant.add_argument("--email", required=True)
    grant.add_argument("--granted-by", required=True)
    grant.add_argument("--reason", required=True)
    grant.add_argument("--expires-days", type=int, default=90)
    grant.add_argument("--metadata-only", action="store_true")

    revoke = subparsers.add_parser("revoke")
    revoke.add_argument("--email", required=True)
    revoke.add_argument("--revoked-by", required=True)
    revoke.add_argument("--reason", required=True)

    subparsers.add_parser("list")
    args = parser.parse_args()
    reason = getattr(args, "reason", "")
    if reason and not 10 <= len(reason.strip()) <= 1000:
        raise SystemExit("Reason must be between 10 and 1000 characters")
    if args.command == "grant" and not 1 <= args.expires_days <= 365:
        raise SystemExit("--expires-days must be between 1 and 365")

    engine = create_engine(_database_url(), connect_args=connect_args_for(_database_url()))
    db = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    try:
        if args.command == "grant":
            target = _user(db, args.email)
            actor = _user(db, args.granted_by)
            now = datetime.now(timezone.utc)
            for existing in (
                db.query(LogAdminAccessGrant)
                .filter(
                    LogAdminAccessGrant.user_id == target.id,
                    LogAdminAccessGrant.revoked_at.is_(None),
                )
                .all()
            ):
                existing.revoked_at = now
                existing.revoked_by_user_id = actor.id
                existing.revoke_reason = "Superseded by a new access grant"
            row = LogAdminAccessGrant(
                id=uuid.uuid4(),
                user_id=target.id,
                granted_by_user_id=actor.id,
                can_read=True,
                can_decrypt=not args.metadata_only,
                grant_reason=args.reason.strip(),
                expires_at=now + timedelta(days=args.expires_days),
            )
            db.add(row)
            db.add(
                LogAccessEvent(
                    id=uuid.uuid4(),
                    actor_user_id=actor.id,
                    action="grant_access",
                    outcome="success",
                    reason=args.reason.strip(),
                    details={
                        "target_user_id": target.id,
                        "can_decrypt": not args.metadata_only,
                        "expires_days": args.expires_days,
                    },
                )
            )
            db.commit()
            print(f"Granted request-log access to user_id={target.id}; expires={row.expires_at.isoformat()}")
        elif args.command == "revoke":
            target = _user(db, args.email)
            actor = _user(db, args.revoked_by)
            rows = (
                db.query(LogAdminAccessGrant)
                .filter(
                    LogAdminAccessGrant.user_id == target.id,
                    LogAdminAccessGrant.revoked_at.is_(None),
                )
                .all()
            )
            now = datetime.now(timezone.utc)
            for row in rows:
                row.revoked_at = now
                row.revoked_by_user_id = actor.id
                row.revoke_reason = args.reason.strip()
            db.add(
                LogAccessEvent(
                    id=uuid.uuid4(),
                    actor_user_id=actor.id,
                    action="revoke_access",
                    outcome="success",
                    reason=args.reason.strip(),
                    details={"target_user_id": target.id, "grants_revoked": len(rows)},
                )
            )
            db.commit()
            print(f"Revoked {len(rows)} request-log grant(s) for user_id={target.id}")
        else:
            rows = db.query(LogAdminAccessGrant).order_by(LogAdminAccessGrant.created_at.desc()).all()
            for row in rows:
                state = "revoked" if row.revoked_at else "active"
                print(
                    f"{row.id} user_id={row.user_id} state={state} "
                    f"decrypt={row.can_decrypt} expires={row.expires_at}"
                )
    finally:
        db.close()
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
