#!/usr/bin/env python3
"""Create/check the least-privilege PostgreSQL roles used by request logging.

Run this only with the migration/database-owner URL. Passwords are read from
environment variables so they do not leak through shell history or process
listings.
"""

from __future__ import annotations

import argparse
import os
import sys

import psycopg2
from psycopg2 import sql


WRITER_ROLE = "editube_log_writer"
READER_ROLE = "editube_log_reader"


def _database_url() -> str:
    url = (os.getenv("LOG_MIGRATION_DATABASE_URL") or os.getenv("DATABASE_URL") or "").strip()
    if not url:
        raise SystemExit("Set LOG_MIGRATION_DATABASE_URL (preferred) or DATABASE_URL")
    return url


def _password(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if len(value) < 24:
        raise SystemExit(f"{name} must contain at least 24 characters")
    return value


def _role_exists(cursor, role: str) -> bool:  # noqa: ANN001
    cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
    return cursor.fetchone() is not None


def _create_or_rotate_role(cursor, role: str, password: str, *, rotate: bool) -> None:  # noqa: ANN001
    identifier = sql.Identifier(role)
    if not _role_exists(cursor, role):
        cursor.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB "
                "NOCREATEROLE NOREPLICATION NOINHERIT"
            ).format(identifier, sql.Literal(password))
        )
    else:
        cursor.execute(
            """
            SELECT parent.rolname
              FROM pg_auth_members membership
              JOIN pg_roles child ON child.oid = membership.member
              JOIN pg_roles parent ON parent.oid = membership.roleid
             WHERE child.rolname = %s
            """,
            (role,),
        )
        memberships = [row[0] for row in cursor.fetchall()]
        if memberships:
            raise SystemExit(
                f"Refusing existing role {role}: it inherits membership in {memberships}. "
                "Create a clean SQL role instead."
            )
        cursor.execute(
            sql.SQL(
                "ALTER ROLE {} NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOINHERIT"
            ).format(identifier)
        )
        if rotate:
            cursor.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(identifier, sql.Literal(password))
            )


def _grant_permissions(cursor) -> None:  # noqa: ANN001
    cursor.execute("SELECT current_database()")
    database = cursor.fetchone()[0]
    cursor.execute(
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}, {}").format(
            sql.Identifier(database),
            sql.Identifier(WRITER_ROLE),
            sql.Identifier(READER_ROLE),
        )
    )
    cursor.execute("SELECT to_regclass('log.api_requests')")
    if cursor.fetchone()[0] is None:
        raise SystemExit("Run `python -m alembic upgrade head` before granting log-table permissions")
    cursor.execute(sql.SQL("REVOKE ALL ON SCHEMA log FROM {}, {}").format(
        sql.Identifier(WRITER_ROLE), sql.Identifier(READER_ROLE)
    ))
    cursor.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA log FROM {}, {}").format(
        sql.Identifier(WRITER_ROLE), sql.Identifier(READER_ROLE)
    ))
    cursor.execute(sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA log FROM {}, {}").format(
        sql.Identifier(WRITER_ROLE), sql.Identifier(READER_ROLE)
    ))
    cursor.execute(sql.SQL("REVOKE ALL ON FUNCTION log.maintain_request_logs() FROM {}, {}").format(
        sql.Identifier(WRITER_ROLE), sql.Identifier(READER_ROLE)
    ))
    cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA log TO {}, {}").format(
        sql.Identifier(WRITER_ROLE), sql.Identifier(READER_ROLE)
    ))
    cursor.execute(sql.SQL(
        "GRANT INSERT ON log.api_requests, log.api_payloads TO {}"
    ).format(sql.Identifier(WRITER_ROLE)))
    cursor.execute(sql.SQL(
        "GRANT EXECUTE ON FUNCTION log.maintain_request_logs() TO {}"
    ).format(sql.Identifier(WRITER_ROLE)))
    cursor.execute(sql.SQL(
        "GRANT SELECT ON log.api_requests, log.api_payloads, log.access_events, "
        "log.admin_access_grants, log.api_request_daily_rollups, log.retention_policy TO {}"
    ).format(sql.Identifier(READER_ROLE)))
    cursor.execute(sql.SQL("GRANT INSERT ON log.access_events TO {}").format(
        sql.Identifier(READER_ROLE)
    ))


def _check(cursor) -> bool:  # noqa: ANN001
    for role in (WRITER_ROLE, READER_ROLE):
        if not _role_exists(cursor, role):
            print(f"FAIL missing role: {role}")
            return False
        cursor.execute(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolinherit "
            "FROM pg_roles WHERE rolname = %s",
            (role,),
        )
        attributes = cursor.fetchone()
        safe_attributes = attributes == (False, False, False, False, False)
        print(f"{'ok' if safe_attributes else 'FAIL':4} {role} restricted attributes: {safe_attributes}")
        if not safe_attributes:
            return False
    checks = [
        (WRITER_ROLE, "log.api_requests", "INSERT", True),
        (WRITER_ROLE, "log.api_requests", "SELECT", False),
        (WRITER_ROLE, "log.api_payloads", "INSERT", True),
        (WRITER_ROLE, "log.api_payloads", "SELECT", False),
        (READER_ROLE, "log.api_requests", "SELECT", True),
        (READER_ROLE, "log.api_requests", "INSERT", False),
        (READER_ROLE, "log.api_payloads", "SELECT", True),
        (READER_ROLE, "log.access_events", "INSERT", True),
        (READER_ROLE, "log.access_events", "UPDATE", False),
        (READER_ROLE, "log.access_events", "DELETE", False),
        (READER_ROLE, "log.admin_access_grants", "INSERT", False),
    ]
    ok = True
    for role in (WRITER_ROLE, READER_ROLE):
        for privilege, expected in (("USAGE", True), ("CREATE", False)):
            cursor.execute(
                "SELECT has_schema_privilege(%s, 'log', %s)",
                (role, privilege),
            )
            actual = bool(cursor.fetchone()[0])
            state = "ok" if actual == expected else "FAIL"
            print(f"{state:4} {role} {privilege} schema log: {actual}")
            ok = ok and actual == expected
    for role, table, privilege, expected in checks:
        cursor.execute("SELECT has_table_privilege(%s, %s, %s)", (role, table, privilege))
        actual = bool(cursor.fetchone()[0])
        state = "ok" if actual == expected else "FAIL"
        print(f"{state:4} {role} {privilege} {table}: {actual}")
        ok = ok and actual == expected
    cursor.execute(
        "SELECT has_function_privilege(%s, 'log.maintain_request_logs()', 'EXECUTE')",
        (WRITER_ROLE,),
    )
    writer_maintenance = bool(cursor.fetchone()[0])
    print(f"{'ok' if writer_maintenance else 'FAIL':4} {WRITER_ROLE} EXECUTE maintenance: {writer_maintenance}")
    ok = ok and writer_maintenance
    cursor.execute(
        "SELECT has_function_privilege(%s, 'log.maintain_request_logs()', 'EXECUTE')",
        (READER_ROLE,),
    )
    reader_maintenance = bool(cursor.fetchone()[0])
    print(f"{'ok' if not reader_maintenance else 'FAIL':4} {READER_ROLE} EXECUTE maintenance: {reader_maintenance}")
    ok = ok and not reader_maintenance
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--create-roles", action="store_true")
    parser.add_argument("--rotate-passwords", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.rotate_passwords and not args.create_roles:
        parser.error("--rotate-passwords requires --create-roles")

    connection = psycopg2.connect(_database_url(), connect_timeout=10)
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '10s'")
                if args.create_roles:
                    _create_or_rotate_role(
                        cursor,
                        WRITER_ROLE,
                        _password("LOG_WRITER_ROLE_PASSWORD"),
                        rotate=args.rotate_passwords,
                    )
                    _create_or_rotate_role(
                        cursor,
                        READER_ROLE,
                        _password("LOG_READER_ROLE_PASSWORD"),
                        rotate=args.rotate_passwords,
                    )
                    _grant_permissions(cursor)
                should_check = args.check or not args.create_roles
                if should_check and not _check(cursor):
                    return 1
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
