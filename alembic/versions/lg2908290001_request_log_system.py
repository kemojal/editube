"""Add encrypted API request logging subsystem.

Revision ID: lg2908290001
Revises: an2808280001
Create Date: 2026-08-29
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "lg2908290001"
down_revision = "an2808280001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS log")
    op.execute("REVOKE ALL ON SCHEMA log FROM PUBLIC")

    op.create_table(
        "api_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=False),
        sa.Column("release", sa.String(length=128), nullable=True),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("route_template", sa.String(length=1024), nullable=True),
        sa.Column("endpoint_name", sa.String(length=256), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("request_size_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("response_size_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("request_content_type", sa.String(length=256), nullable=True),
        sa.Column("response_content_type", sa.String(length=256), nullable=True),
        sa.Column("request_headers", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("response_headers", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("client_ip_hash", sa.String(length=64), nullable=True),
        sa.Column("user_agent_hash", sa.String(length=64), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("workspace_id", sa.Integer(), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("capture_reason", sa.String(length=64), nullable=False),
        sa.Column("request_body_state", sa.String(length=32), nullable=False),
        sa.Column("response_body_state", sa.String(length=32), nullable=False),
        sa.Column("error_class", sa.String(length=128), nullable=True),
        sa.Column("payload_present", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint("status_code BETWEEN 100 AND 599", name="ck_log_api_requests_status"),
        sa.CheckConstraint("duration_ms >= 0", name="ck_log_api_requests_duration"),
        sa.PrimaryKeyConstraint("id"),
        schema="log",
    )
    op.create_index("ix_log_api_requests_request_id", "api_requests", ["request_id"], schema="log")
    op.create_index("ix_log_api_requests_occurred_at", "api_requests", ["occurred_at"], schema="log")
    op.create_index("ix_log_api_requests_environment", "api_requests", ["environment"], schema="log")
    op.create_index(
        "ix_log_api_requests_route_status",
        "api_requests",
        ["route_template", "status_code"],
        schema="log",
    )
    op.create_index(
        "ix_log_api_requests_user_time", "api_requests", ["user_id", "occurred_at"], schema="log"
    )
    op.create_index(
        "ix_log_api_requests_workspace_time",
        "api_requests",
        ["workspace_id", "occurred_at"],
        schema="log",
    )

    op.create_table(
        "api_payloads",
        sa.Column("request_log_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key_id", sa.String(length=128), nullable=False),
        sa.Column("algorithm", sa.String(length=32), server_default="fernet", nullable=False),
        sa.Column("request_ciphertext", sa.Text(), nullable=True),
        sa.Column("request_sha256", sa.String(length=64), nullable=True),
        sa.Column("response_ciphertext", sa.Text(), nullable=True),
        sa.Column("response_sha256", sa.String(length=64), nullable=True),
        sa.Column("redaction_summary", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_log_id"], ["log.api_requests.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("request_log_id"),
        schema="log",
    )
    op.create_index("ix_log_api_payloads_expires_at", "api_payloads", ["expires_at"], schema="log")

    op.create_table(
        "access_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("request_log_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("client_ip_hash", sa.String(length=64), nullable=True),
        sa.Column("user_agent_hash", sa.String(length=64), nullable=True),
        sa.Column("details", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        schema="log",
    )
    op.create_index(
        "ix_log_access_events_actor_time",
        "access_events",
        ["actor_user_id", "created_at"],
        schema="log",
    )
    op.create_index(
        "ix_log_access_events_request_log_id", "access_events", ["request_log_id"], schema="log"
    )

    op.create_table(
        "admin_access_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("granted_by_user_id", sa.Integer(), nullable=False),
        sa.Column("can_read", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("can_decrypt", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("grant_reason", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoked_by_user_id", sa.Integer(), nullable=True),
        sa.Column("revoke_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("char_length(grant_reason) >= 10", name="ck_log_grant_reason"),
        sa.PrimaryKeyConstraint("id"),
        schema="log",
    )
    op.create_index(
        "ix_log_admin_grants_user_active",
        "admin_access_grants",
        ["user_id", "revoked_at", "expires_at"],
        schema="log",
    )
    op.create_index(
        "uq_log_admin_grants_one_unrevoked",
        "admin_access_grants",
        ["user_id"],
        unique=True,
        schema="log",
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.create_table(
        "api_request_daily_rollups",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=False),
        sa.Column("route_template", sa.String(length=1024), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("status_class", sa.String(length=3), nullable=False),
        sa.Column("request_count", sa.BigInteger(), nullable=False),
        sa.Column("failure_count", sa.BigInteger(), nullable=False),
        sa.Column("average_duration_ms", sa.Float(), nullable=False),
        sa.Column("p95_duration_ms", sa.Float(), nullable=False),
        sa.Column("average_request_bytes", sa.Float(), nullable=False),
        sa.Column("average_response_bytes", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "day", "environment", "route_template", "method", "status_class",
            name="uq_log_daily_route_method_status",
        ),
        schema="log",
    )
    op.create_index(
        "ix_log_api_request_daily_rollups_day",
        "api_request_daily_rollups",
        ["day"],
        schema="log",
    )

    op.create_table(
        "retention_policy",
        sa.Column("singleton_id", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("metadata_retention_days", sa.Integer(), server_default="30", nullable=False),
        sa.Column("access_audit_retention_days", sa.Integer(), server_default="400", nullable=False),
        sa.Column("rollup_retention_days", sa.Integer(), server_default="400", nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("singleton_id", name="ck_log_retention_singleton_true"),
        sa.CheckConstraint(
            "metadata_retention_days BETWEEN 1 AND 365",
            name="ck_log_retention_metadata_days",
        ),
        sa.CheckConstraint(
            "access_audit_retention_days BETWEEN 90 AND 2555",
            name="ck_log_retention_access_days",
        ),
        sa.CheckConstraint(
            "rollup_retention_days BETWEEN 30 AND 2555",
            name="ck_log_retention_rollup_days",
        ),
        sa.PrimaryKeyConstraint("singleton_id"),
        schema="log",
    )
    op.execute(
        """
        INSERT INTO log.retention_policy (
            singleton_id, metadata_retention_days,
            access_audit_retention_days, rollup_retention_days
        ) VALUES (true, 30, 400, 400)
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION log.protect_access_events()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            owner_name name;
        BEGIN
            SELECT pg_get_userbyid(c.relowner)
              INTO owner_name
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'log' AND c.relname = 'access_events';
            IF current_user <> owner_name THEN
                RAISE EXCEPTION 'log.access_events is append-only';
            END IF;
            RETURN OLD;
        END;
        $$;

        CREATE TRIGGER trg_log_access_events_append_only
        BEFORE UPDATE OR DELETE ON log.access_events
        FOR EACH ROW EXECUTE FUNCTION log.protect_access_events();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION log.maintain_request_logs()
        RETURNS TABLE(payloads_deleted bigint, requests_deleted bigint,
                      access_events_deleted bigint, rollups_deleted bigint)
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, log
        AS $$
        DECLARE
            payload_count bigint := 0;
            request_count_deleted bigint := 0;
            access_count bigint := 0;
            rollup_count bigint := 0;
            metadata_days integer;
            access_days integer;
            rollup_days integer;
        BEGIN
            IF NOT pg_try_advisory_xact_lock(587204291864113221::bigint) THEN
                RETURN QUERY SELECT 0::bigint, 0::bigint, 0::bigint, 0::bigint;
                RETURN;
            END IF;
            SELECT metadata_retention_days, access_audit_retention_days,
                   rollup_retention_days
              INTO STRICT metadata_days, access_days, rollup_days
              FROM log.retention_policy
             WHERE singleton_id = true;

            INSERT INTO log.api_request_daily_rollups (
                day, environment, route_template, method, status_class,
                request_count, failure_count, average_duration_ms,
                p95_duration_ms, average_request_bytes, average_response_bytes, updated_at
            )
            SELECT
                (occurred_at AT TIME ZONE 'UTC')::date,
                environment,
                COALESCE(route_template, '<unknown>'),
                method,
                (status_code / 100)::text || 'xx',
                COUNT(*),
                COUNT(*) FILTER (WHERE status_code >= 400),
                AVG(duration_ms)::double precision,
                percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)::double precision,
                AVG(request_size_bytes)::double precision,
                AVG(response_size_bytes)::double precision,
                now()
            FROM log.api_requests
            WHERE occurred_at < date_trunc('day', now())
            GROUP BY 1, 2, 3, 4, 5
            ON CONFLICT (day, environment, route_template, method, status_class)
            DO UPDATE SET
                request_count = EXCLUDED.request_count,
                failure_count = EXCLUDED.failure_count,
                average_duration_ms = EXCLUDED.average_duration_ms,
                p95_duration_ms = EXCLUDED.p95_duration_ms,
                average_request_bytes = EXCLUDED.average_request_bytes,
                average_response_bytes = EXCLUDED.average_response_bytes,
                updated_at = now();

            DELETE FROM log.api_payloads WHERE expires_at < now();
            GET DIAGNOSTICS payload_count = ROW_COUNT;
            DELETE FROM log.api_requests
             WHERE occurred_at < now() - make_interval(days => metadata_days);
            GET DIAGNOSTICS request_count_deleted = ROW_COUNT;
            DELETE FROM log.access_events
             WHERE created_at < now() - make_interval(days => access_days);
            GET DIAGNOSTICS access_count = ROW_COUNT;
            DELETE FROM log.api_request_daily_rollups
             WHERE day < current_date - rollup_days;
            GET DIAGNOSTICS rollup_count = ROW_COUNT;

            RETURN QUERY SELECT payload_count, request_count_deleted, access_count, rollup_count;
        END;
        $$;
        REVOKE ALL ON FUNCTION log.maintain_request_logs() FROM PUBLIC;
        """
    )

    op.execute(
        """
        REVOKE ALL ON ALL TABLES IN SCHEMA log FROM PUBLIC;
        REVOKE ALL ON ALL SEQUENCES IN SCHEMA log FROM PUBLIC;
        ALTER DEFAULT PRIVILEGES IN SCHEMA log REVOKE ALL ON TABLES FROM PUBLIC;
        ALTER DEFAULT PRIVILEGES IN SCHEMA log REVOKE ALL ON SEQUENCES FROM PUBLIC;

        DO $roles$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'editube_log_writer') THEN
                IF EXISTS (
                    SELECT 1
                      FROM pg_roles role
                      LEFT JOIN pg_auth_members membership ON membership.member = role.oid
                     WHERE role.rolname = 'editube_log_writer'
                       AND (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                            OR role.rolreplication OR role.rolinherit
                            OR membership.roleid IS NOT NULL)
                ) THEN
                    RAISE EXCEPTION 'editube_log_writer has unsafe attributes or memberships';
                END IF;
                GRANT USAGE ON SCHEMA log TO editube_log_writer;
                GRANT INSERT ON log.api_requests, log.api_payloads TO editube_log_writer;
                GRANT EXECUTE ON FUNCTION log.maintain_request_logs()
                    TO editube_log_writer;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'editube_log_reader') THEN
                IF EXISTS (
                    SELECT 1
                      FROM pg_roles role
                      LEFT JOIN pg_auth_members membership ON membership.member = role.oid
                     WHERE role.rolname = 'editube_log_reader'
                       AND (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                            OR role.rolreplication OR role.rolinherit
                            OR membership.roleid IS NOT NULL)
                ) THEN
                    RAISE EXCEPTION 'editube_log_reader has unsafe attributes or memberships';
                END IF;
                GRANT USAGE ON SCHEMA log TO editube_log_reader;
                GRANT SELECT ON log.api_requests, log.api_payloads, log.access_events,
                    log.admin_access_grants, log.api_request_daily_rollups,
                    log.retention_policy
                    TO editube_log_reader;
                GRANT INSERT ON log.access_events TO editube_log_reader;
            END IF;
        END
        $roles$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS log.maintain_request_logs()")
    op.execute("DROP TRIGGER IF EXISTS trg_log_access_events_append_only ON log.access_events")
    op.execute("DROP FUNCTION IF EXISTS log.protect_access_events()")
    op.drop_table("retention_policy", schema="log")
    op.drop_table("api_request_daily_rollups", schema="log")
    op.drop_table("admin_access_grants", schema="log")
    op.drop_table("access_events", schema="log")
    op.drop_table("api_payloads", schema="log")
    op.drop_table("api_requests", schema="log")
    op.execute("DROP SCHEMA IF EXISTS log")
