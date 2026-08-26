"""stripe webhook idempotency ledger + users.selected_plan

NOTE: this does not retroactively downgrade accounts whose `users.plan` is
`pro`/`scale` without a matching entitled subscription — the population is a
mix of genuine customers whose `subscriptions` rows predate the table and
accounts that self-granted through the old `PUT /users/onboarding/plan`, and
the two are not distinguishable from schema alone. Reconcile against Stripe
before running any cleanup.


Stripe delivers every event at least once and retries anything that does not
return 2xx. The webhook handler's database writes were already idempotent
(upserts keyed on the subscription id), but its side effects were not — a
retried ``checkout.session.completed`` sent a second welcome email, and a
retried ``customer.subscription.updated`` re-sent the "will not renew" notice.

One row per processed event id, written only after the handler succeeds, makes
a replay a cheap no-op.

Revision ID: d8e9f0a1b2c3
Revises: b7c8d9e0f1a2
Create Date: 2026-08-16
"""

from alembic import op
import sqlalchemy as sa

revision = "d8e9f0a1b2c3"
down_revision = "b7c8d9e0f1a2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "stripe_webhook_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("stripe_event_id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=True),
        sa.Column(
            "processed_at",
            sa.TIMESTAMP(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    # Unique, not merely indexed: the constraint violation on a concurrent
    # redelivery is what serialises two workers racing on the same event.
    op.create_index(
        "ix_stripe_webhook_events_stripe_event_id",
        "stripe_webhook_events",
        ["stripe_event_id"],
        unique=True,
    )

    # Separates "the tier this account picked in onboarding" from "the tier it
    # is entitled to". `PUT /users/onboarding/plan` wrote the latter, so any
    # authenticated user could grant themselves Scale quotas for free.
    op.add_column("users", sa.Column("selected_plan", sa.String(), nullable=True))
    # Existing rows: whatever they were showing as their plan was their pick.
    op.execute("UPDATE users SET selected_plan = plan WHERE plan IS NOT NULL")


def downgrade():
    op.drop_column("users", "selected_plan")
    op.drop_index(
        "ix_stripe_webhook_events_stripe_event_id",
        table_name="stripe_webhook_events",
    )
    op.drop_table("stripe_webhook_events")
