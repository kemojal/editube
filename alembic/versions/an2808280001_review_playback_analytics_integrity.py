"""review playback analytics integrity

Revision ID: an2808280001
Revises: af2708270003
Create Date: 2026-08-28
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "an2808280001"
down_revision = "af2708270003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "review_sessions",
        sa.Column(
            "analytics_milestones",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    # Historical client retries may have inserted the same sequence more than
    # once. Keep the first occurrence before enforcing idempotency.
    op.execute(
        """
        DELETE FROM review_events newer
        USING review_events older
        WHERE newer.session_id = older.session_id
          AND newer.seq = older.seq
          AND newer.seq IS NOT NULL
          AND newer.id > older.id
        """
    )
    op.create_unique_constraint(
        "uq_review_events_session_seq",
        "review_events",
        ["session_id", "seq"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_review_events_session_seq", "review_events", type_="unique"
    )
    op.drop_column("review_sessions", "analytics_milestones")
