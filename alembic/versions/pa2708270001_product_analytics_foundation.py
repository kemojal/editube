"""product analytics, consent, feedback, and subscription lifecycle

Revision ID: pa2708270001
Revises: af2708270001
Create Date: 2026-08-27
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "pa2708270001"
down_revision = "af2708270001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("subscriptions", sa.Column("cancellation_requested_at", sa.TIMESTAMP(), nullable=True))
    op.add_column("subscriptions", sa.Column("cancellation_effective_at", sa.TIMESTAMP(), nullable=True))
    op.add_column("subscriptions", sa.Column("cancellation_feedback", sa.String(), nullable=True))
    op.add_column("subscriptions", sa.Column("cancellation_comment_encrypted", sa.Text(), nullable=True))
    op.add_column("subscriptions", sa.Column("cancellation_source", sa.String(), nullable=True))
    op.add_column("subscriptions", sa.Column("voluntary_churn", sa.Boolean(), nullable=True))
    op.add_column("subscriptions", sa.Column("currency", sa.String(), nullable=True))
    op.add_column("subscriptions", sa.Column("unit_amount", sa.BigInteger(), nullable=True))
    op.add_column("subscriptions", sa.Column("quantity", sa.Integer(), nullable=True))
    op.add_column("subscriptions", sa.Column("discount_amount", sa.BigInteger(), nullable=True))
    op.add_column("subscriptions", sa.Column("discount_percent", sa.Float(), nullable=True))
    op.add_column("subscriptions", sa.Column("recurring_interval", sa.String(), nullable=True))
    op.add_column("subscriptions", sa.Column("latest_invoice_id", sa.String(), nullable=True))
    op.create_index("ix_subscriptions_latest_invoice_id", "subscriptions", ["latest_invoice_id"])

    op.create_table(
        "analytics_outbox",
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("event_name", sa.String(), nullable=False),
        sa.Column("schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("occurred_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("environment", sa.String(), nullable=False),
        sa.Column("release", sa.String(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("workspace_id", sa.Integer(), nullable=True),
        sa.Column("anonymous_id", sa.String(), nullable=True),
        sa.Column("properties", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("delivery_status", sa.String(), server_default="pending", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("delivery_started_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("last_error_code", sa.String(), nullable=True),
        sa.Column("delivered_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index("ix_analytics_outbox_event_name", "analytics_outbox", ["event_name"])
    op.create_index("ix_analytics_outbox_occurred_at", "analytics_outbox", ["occurred_at"])
    op.create_index("ix_analytics_outbox_user_id", "analytics_outbox", ["user_id"])
    op.create_index("ix_analytics_outbox_workspace_id", "analytics_outbox", ["workspace_id"])
    op.create_index("ix_analytics_outbox_delivery_status", "analytics_outbox", ["delivery_status"])
    op.create_index("ix_analytics_outbox_next_attempt_at", "analytics_outbox", ["next_attempt_at"])

    op.create_table(
        "analytics_consents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("anonymous_consent_id", sa.String(), nullable=False),
        sa.Column("consent_state", sa.String(), server_default="essential_only", nullable=False),
        sa.Column("analytics_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("replay_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("product_data_improvement_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("consent_version", sa.String(), nullable=False),
        sa.Column("region_policy", sa.String(), server_default="default", nullable=False),
        sa.Column("global_privacy_control", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("consented_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("withdrawn_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("anonymous_consent_id", name="uq_analytics_consent_anonymous"),
    )
    op.create_index("ix_analytics_consents_id", "analytics_consents", ["id"])
    op.create_index("ix_analytics_consents_user_id", "analytics_consents", ["user_id"])

    op.create_table(
        "analytics_consent_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("anonymous_consent_id", sa.String(), nullable=False),
        sa.Column("consent_state", sa.String(), nullable=False),
        sa.Column("analytics_enabled", sa.Boolean(), nullable=False),
        sa.Column("replay_enabled", sa.Boolean(), nullable=False),
        sa.Column("product_data_improvement_enabled", sa.Boolean(), nullable=False),
        sa.Column("consent_version", sa.String(), nullable=False),
        sa.Column("region_policy", sa.String(), nullable=False),
        sa.Column("global_privacy_control", sa.Boolean(), nullable=False),
        sa.Column("occurred_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for name in ("id", "user_id", "anonymous_consent_id", "occurred_at"):
        op.create_index(f"ix_analytics_consent_events_{name}", "analytics_consent_events", [name])

    op.create_table(
        "analytics_feedback",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("workspace_id", sa.Integer(), nullable=True),
        sa.Column("prompt_key", sa.String(), nullable=False),
        sa.Column("reason_code", sa.String(), nullable=False),
        sa.Column("comment_encrypted", sa.Text(), nullable=True),
        sa.Column("route_template", sa.String(), nullable=True),
        sa.Column("feature_key", sa.String(), nullable=True),
        sa.Column("analytics_session_id", sa.String(), nullable=True),
        sa.Column("consent_version", sa.String(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_analytics_feedback_id", "analytics_feedback", ["id"])
    op.create_index("ix_analytics_feedback_user_id", "analytics_feedback", ["user_id"])
    op.create_index("ix_analytics_feedback_workspace_id", "analytics_feedback", ["workspace_id"])
    op.create_index("ix_analytics_feedback_prompt_key", "analytics_feedback", ["prompt_key"])
    op.create_index("ix_analytics_feedback_reason_code", "analytics_feedback", ["reason_code"])
    op.create_index("ix_analytics_feedback_feature_key", "analytics_feedback", ["feature_key"])
    op.create_index("ix_analytics_feedback_created_at", "analytics_feedback", ["created_at"])

    op.create_table(
        "analytics_data_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("distinct_id", sa.String(), nullable=False),
        sa.Column("request_type", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column("provider_status", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_error_code", sa.String(), nullable=True),
        sa.Column("requested_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.TIMESTAMP(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for name in ("id", "user_id", "distinct_id", "request_type", "status"):
        op.create_index(f"ix_analytics_data_requests_{name}", "analytics_data_requests", [name])

    op.create_table(
        "subscription_lifecycle_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_key", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("source_event_id", sa.String(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("workspace_id", sa.Integer(), nullable=True),
        sa.Column("stripe_subscription_id", sa.String(), nullable=True),
        sa.Column("stripe_invoice_id", sa.String(), nullable=True),
        sa.Column("plan", sa.String(), nullable=True),
        sa.Column("previous_plan", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("previous_status", sa.String(), nullable=True),
        sa.Column("currency", sa.String(), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=True),
        sa.Column("recurring_interval", sa.String(), nullable=True),
        sa.Column("voluntary", sa.Boolean(), nullable=True),
        sa.Column("reason_code", sa.String(), nullable=True),
        sa.Column("effective_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("meta_info", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("occurred_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_key"),
    )
    for name in (
        "event_key",
        "event_type",
        "source_event_id",
        "user_id",
        "workspace_id",
        "stripe_subscription_id",
        "stripe_invoice_id",
        "occurred_at",
    ):
        op.create_index(f"ix_subscription_lifecycle_events_{name}", "subscription_lifecycle_events", [name])

    op.create_table(
        "workspace_activations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("feature_key", sa.String(), nullable=False),
        sa.Column("resource_type", sa.String(), nullable=True),
        sa.Column("resource_id", sa.String(), nullable=True),
        sa.Column("achieved_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", name="uq_workspace_activation_workspace"),
    )
    for name in ("id", "workspace_id", "user_id", "feature_key", "achieved_at"):
        op.create_index(f"ix_workspace_activations_{name}", "workspace_activations", [name])


def downgrade() -> None:
    op.drop_table("workspace_activations")
    op.drop_table("subscription_lifecycle_events")
    op.drop_table("analytics_data_requests")
    op.drop_table("analytics_feedback")
    op.drop_table("analytics_consent_events")
    op.drop_table("analytics_consents")
    op.drop_table("analytics_outbox")

    op.drop_index("ix_subscriptions_latest_invoice_id", table_name="subscriptions")
    for column in (
        "latest_invoice_id",
        "recurring_interval",
        "discount_percent",
        "discount_amount",
        "quantity",
        "unit_amount",
        "currency",
        "voluntary_churn",
        "cancellation_source",
        "cancellation_comment_encrypted",
        "cancellation_feedback",
        "cancellation_effective_at",
        "cancellation_requested_at",
    ):
        op.drop_column("subscriptions", column)
