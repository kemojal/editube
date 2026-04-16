"""enterprise security foundation

Revision ID: m1n2o3p4q5r6
Revises: l4m5n6o7p8q9
Create Date: 2026-04-15 17:10:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "m1n2o3p4q5r6"
down_revision = "l4m5n6o7p8q9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("mfa_required", sa.Boolean(), server_default=sa.text("false"), nullable=False))

    op.add_column("review_links", sa.Column("watermark_mode", sa.String(), server_default="visible_overlay", nullable=False))
    op.add_column("review_links", sa.Column("nda_required", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.add_column("review_links", sa.Column("nda_document_id", sa.Integer(), nullable=True))
    op.add_column("review_links", sa.Column("geofence_mode", sa.String(), server_default="off", nullable=False))
    op.add_column("review_links", sa.Column("geo_allow_countries", postgresql.ARRAY(sa.String()), nullable=True))
    op.add_column("review_links", sa.Column("geo_block_countries", postgresql.ARRAY(sa.String()), nullable=True))
    op.add_column("review_links", sa.Column("recording_detection_mode", sa.String(), server_default="monitor", nullable=False))
    op.add_column("review_links", sa.Column("revocation_reason", sa.String(), nullable=True))

    op.add_column("review_sessions", sa.Column("country_code", sa.String(), nullable=True))
    op.add_column("review_sessions", sa.Column("watermark_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True))

    op.add_column("review_events", sa.Column("seq", sa.Integer(), nullable=True))
    op.add_column("review_events", sa.Column("meta_info", postgresql.JSONB(astext_type=sa.Text()), nullable=True))

    op.create_table(
        "security_audit_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=True),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("video_id", sa.Integer(), nullable=True),
        sa.Column("review_link_id", sa.Integer(), nullable=True),
        sa.Column("session_id", sa.Integer(), nullable=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("actor_type", sa.String(), server_default="system", nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("resource_type", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String(), nullable=True),
        sa.Column("outcome", sa.String(), server_default="success", nullable=False),
        sa.Column("ip_address", sa.String(), nullable=True),
        sa.Column("country_code", sa.String(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["review_link_id"], ["review_links.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["session_id"], ["review_sessions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_security_audit_logs_id"), "security_audit_logs", ["id"], unique=False)
    op.create_index(op.f("ix_security_audit_logs_workspace_id"), "security_audit_logs", ["workspace_id"], unique=False)
    op.create_index(op.f("ix_security_audit_logs_project_id"), "security_audit_logs", ["project_id"], unique=False)
    op.create_index(op.f("ix_security_audit_logs_video_id"), "security_audit_logs", ["video_id"], unique=False)
    op.create_index(op.f("ix_security_audit_logs_review_link_id"), "security_audit_logs", ["review_link_id"], unique=False)
    op.create_index(op.f("ix_security_audit_logs_session_id"), "security_audit_logs", ["session_id"], unique=False)
    op.create_index(op.f("ix_security_audit_logs_actor_user_id"), "security_audit_logs", ["actor_user_id"], unique=False)
    op.create_index(op.f("ix_security_audit_logs_action"), "security_audit_logs", ["action"], unique=False)
    op.create_index(op.f("ix_security_audit_logs_country_code"), "security_audit_logs", ["country_code"], unique=False)
    op.create_index(op.f("ix_security_audit_logs_created_at"), "security_audit_logs", ["created_at"], unique=False)

    op.create_table(
        "nda_documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("body_markdown", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_nda_documents_id"), "nda_documents", ["id"], unique=False)
    op.create_index(op.f("ix_nda_documents_workspace_id"), "nda_documents", ["workspace_id"], unique=False)
    op.create_index(op.f("ix_nda_documents_content_sha256"), "nda_documents", ["content_sha256"], unique=False)

    op.create_table(
        "nda_acceptances",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("review_link_id", sa.Integer(), nullable=False),
        sa.Column("nda_document_id", sa.Integer(), nullable=False),
        sa.Column("identity_key", sa.String(), nullable=False),
        sa.Column("guest_name", sa.String(), nullable=True),
        sa.Column("guest_email", sa.String(), nullable=True),
        sa.Column("ip_address", sa.String(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("accepted_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["nda_document_id"], ["nda_documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["review_link_id"], ["review_links.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("review_link_id", "identity_key", "nda_document_id", name="uq_nda_acceptance_link_identity_doc"),
    )
    op.create_index(op.f("ix_nda_acceptances_id"), "nda_acceptances", ["id"], unique=False)
    op.create_index(op.f("ix_nda_acceptances_review_link_id"), "nda_acceptances", ["review_link_id"], unique=False)
    op.create_index(op.f("ix_nda_acceptances_nda_document_id"), "nda_acceptances", ["nda_document_id"], unique=False)
    op.create_index(op.f("ix_nda_acceptances_identity_key"), "nda_acceptances", ["identity_key"], unique=False)
    op.create_index(op.f("ix_nda_acceptances_guest_email"), "nda_acceptances", ["guest_email"], unique=False)
    op.create_index(op.f("ix_nda_acceptances_accepted_at"), "nda_acceptances", ["accepted_at"], unique=False)

    op.create_table(
        "user_mfa_methods",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("method_type", sa.String(), server_default="totp", nullable=False),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column("secret_encrypted", sa.Text(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("verified_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("disabled_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_user_mfa_methods_id"), "user_mfa_methods", ["id"], unique=False)
    op.create_index(op.f("ix_user_mfa_methods_user_id"), "user_mfa_methods", ["user_id"], unique=False)

    op.create_table(
        "user_mfa_recovery_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("code_hash", sa.String(), nullable=False),
        sa.Column("used_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_user_mfa_recovery_codes_id"), "user_mfa_recovery_codes", ["id"], unique=False)
    op.create_index(op.f("ix_user_mfa_recovery_codes_user_id"), "user_mfa_recovery_codes", ["user_id"], unique=False)
    op.create_index(op.f("ix_user_mfa_recovery_codes_code_hash"), "user_mfa_recovery_codes", ["code_hash"], unique=True)

    op.create_table(
        "workspace_sso_providers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("issuer", sa.String(), nullable=False),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("client_secret_encrypted", sa.Text(), nullable=False),
        sa.Column("authorization_endpoint", sa.String(), nullable=True),
        sa.Column("token_endpoint", sa.String(), nullable=True),
        sa.Column("userinfo_endpoint", sa.String(), nullable=True),
        sa.Column("jwks_uri", sa.String(), nullable=True),
        sa.Column("scope", sa.String(), server_default="openid profile email", nullable=False),
        sa.Column("domain_hint", sa.String(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_workspace_sso_providers_id"), "workspace_sso_providers", ["id"], unique=False)
    op.create_index(op.f("ix_workspace_sso_providers_workspace_id"), "workspace_sso_providers", ["workspace_id"], unique=False)
    op.create_index(op.f("ix_workspace_sso_providers_domain_hint"), "workspace_sso_providers", ["domain_hint"], unique=False)

    op.create_table(
        "workspace_auth_policies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("enforce_sso", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("allowed_login_methods", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("mfa_required", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id"),
    )
    op.create_index(op.f("ix_workspace_auth_policies_id"), "workspace_auth_policies", ["id"], unique=False)
    op.create_index(op.f("ix_workspace_auth_policies_workspace_id"), "workspace_auth_policies", ["workspace_id"], unique=True)

    op.create_table(
        "review_forensic_assets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("review_link_id", sa.Integer(), nullable=False),
        sa.Column("review_session_id", sa.Integer(), nullable=False),
        sa.Column("watermark_fingerprint", sa.String(), nullable=False),
        sa.Column("playback_manifest_url", sa.Text(), nullable=True),
        sa.Column("package_status", sa.String(), server_default="pending", nullable=False),
        sa.Column("package_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["review_link_id"], ["review_links.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["review_session_id"], ["review_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_review_forensic_assets_id"), "review_forensic_assets", ["id"], unique=False)
    op.create_index(op.f("ix_review_forensic_assets_review_link_id"), "review_forensic_assets", ["review_link_id"], unique=False)
    op.create_index(op.f("ix_review_forensic_assets_review_session_id"), "review_forensic_assets", ["review_session_id"], unique=False)
    op.create_index(op.f("ix_review_forensic_assets_watermark_fingerprint"), "review_forensic_assets", ["watermark_fingerprint"], unique=False)

    op.create_foreign_key("fk_review_links_nda_document_id", "review_links", "nda_documents", ["nda_document_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    op.drop_constraint("fk_review_links_nda_document_id", "review_links", type_="foreignkey")

    op.drop_index(op.f("ix_review_forensic_assets_watermark_fingerprint"), table_name="review_forensic_assets")
    op.drop_index(op.f("ix_review_forensic_assets_review_session_id"), table_name="review_forensic_assets")
    op.drop_index(op.f("ix_review_forensic_assets_review_link_id"), table_name="review_forensic_assets")
    op.drop_index(op.f("ix_review_forensic_assets_id"), table_name="review_forensic_assets")
    op.drop_table("review_forensic_assets")

    op.drop_index(op.f("ix_workspace_auth_policies_workspace_id"), table_name="workspace_auth_policies")
    op.drop_index(op.f("ix_workspace_auth_policies_id"), table_name="workspace_auth_policies")
    op.drop_table("workspace_auth_policies")

    op.drop_index(op.f("ix_workspace_sso_providers_domain_hint"), table_name="workspace_sso_providers")
    op.drop_index(op.f("ix_workspace_sso_providers_workspace_id"), table_name="workspace_sso_providers")
    op.drop_index(op.f("ix_workspace_sso_providers_id"), table_name="workspace_sso_providers")
    op.drop_table("workspace_sso_providers")

    op.drop_index(op.f("ix_user_mfa_recovery_codes_code_hash"), table_name="user_mfa_recovery_codes")
    op.drop_index(op.f("ix_user_mfa_recovery_codes_user_id"), table_name="user_mfa_recovery_codes")
    op.drop_index(op.f("ix_user_mfa_recovery_codes_id"), table_name="user_mfa_recovery_codes")
    op.drop_table("user_mfa_recovery_codes")

    op.drop_index(op.f("ix_user_mfa_methods_user_id"), table_name="user_mfa_methods")
    op.drop_index(op.f("ix_user_mfa_methods_id"), table_name="user_mfa_methods")
    op.drop_table("user_mfa_methods")

    op.drop_index(op.f("ix_nda_acceptances_accepted_at"), table_name="nda_acceptances")
    op.drop_index(op.f("ix_nda_acceptances_guest_email"), table_name="nda_acceptances")
    op.drop_index(op.f("ix_nda_acceptances_identity_key"), table_name="nda_acceptances")
    op.drop_index(op.f("ix_nda_acceptances_nda_document_id"), table_name="nda_acceptances")
    op.drop_index(op.f("ix_nda_acceptances_review_link_id"), table_name="nda_acceptances")
    op.drop_index(op.f("ix_nda_acceptances_id"), table_name="nda_acceptances")
    op.drop_table("nda_acceptances")

    op.drop_index(op.f("ix_nda_documents_content_sha256"), table_name="nda_documents")
    op.drop_index(op.f("ix_nda_documents_workspace_id"), table_name="nda_documents")
    op.drop_index(op.f("ix_nda_documents_id"), table_name="nda_documents")
    op.drop_table("nda_documents")

    op.drop_index(op.f("ix_security_audit_logs_created_at"), table_name="security_audit_logs")
    op.drop_index(op.f("ix_security_audit_logs_country_code"), table_name="security_audit_logs")
    op.drop_index(op.f("ix_security_audit_logs_action"), table_name="security_audit_logs")
    op.drop_index(op.f("ix_security_audit_logs_actor_user_id"), table_name="security_audit_logs")
    op.drop_index(op.f("ix_security_audit_logs_session_id"), table_name="security_audit_logs")
    op.drop_index(op.f("ix_security_audit_logs_review_link_id"), table_name="security_audit_logs")
    op.drop_index(op.f("ix_security_audit_logs_video_id"), table_name="security_audit_logs")
    op.drop_index(op.f("ix_security_audit_logs_project_id"), table_name="security_audit_logs")
    op.drop_index(op.f("ix_security_audit_logs_workspace_id"), table_name="security_audit_logs")
    op.drop_index(op.f("ix_security_audit_logs_id"), table_name="security_audit_logs")
    op.drop_table("security_audit_logs")

    op.drop_column("review_events", "meta_info")
    op.drop_column("review_events", "seq")
    op.drop_column("review_sessions", "watermark_payload")
    op.drop_column("review_sessions", "country_code")

    op.drop_column("review_links", "revocation_reason")
    op.drop_column("review_links", "recording_detection_mode")
    op.drop_column("review_links", "geo_block_countries")
    op.drop_column("review_links", "geo_allow_countries")
    op.drop_column("review_links", "geofence_mode")
    op.drop_column("review_links", "nda_document_id")
    op.drop_column("review_links", "nda_required")
    op.drop_column("review_links", "watermark_mode")

    op.drop_column("users", "mfa_required")
